"""Ready-job selection and dispatch mechanics.

The scheduler decides *what may run now*; the supervisor decides *what a result
means*. Keeping those apart is what lets the dry-run reuse the exact selection
logic without any risk of side effects.
"""

from . import artifacts, config, experiments, gates, integrity, landing, metrics
from .context import ContextCompiler
from .errors import LeaseError
from .policy import permissions as permission_policy
from .policy import tools as tool_policy
from .policy.routing import ModelRouter
from .providers import RunRequest
from .providers.base import RUN_FAILED, RunOutcome
from .state import leases, machine


class Scheduler:
    def __init__(self, store, provider, compiler=None, owner="supervisor-1",
                 concurrency=1, lease_ttl=900, repo_facts=None, dry_run=False,
                 router=None, permission_policy_body=None, shared_paths=()):
        self.store = store
        self.provider = provider
        self.router = router or ModelRouter(store, provider_name=provider.name)
        self.compiler = compiler or ContextCompiler(store)
        self.owner = owner
        self.concurrency = concurrency
        self.lease_ttl = lease_ttl
        self.repo_facts = repo_facts or {}
        self.dry_run = dry_run
        self.permission_policy = permission_policy_body or permission_policy.DEFAULT_POLICY
        # Checkouts a bypassed worker must never be pointed at.
        self.shared_paths = tuple(shared_paths)

    # --- selection --------------------------------------------------------

    def ready_jobs(self, project_id=None, limit=None):
        """Jobs that may run right now: dependencies met, unleased, ungated."""
        leases.reclaim_expired(self.store)
        leases.recover_orphans(self.store, project_id)
        self.store.promote_ready(project_id)
        selected = []
        for job in self.store.list_jobs(project_id, status=machine.READY):
            if leases.holder(self.store, job["id"]):
                continue
            if gates.open_gates(self.store, job_id=job["id"]):
                continue
            selected.append(job)
            if limit and len(selected) >= limit:
                break
        return selected

    def candidate_jobs(self, project_id=None):
        """What *would* be ready, without promoting anything. Dry-run safe."""
        candidates = list(self.store.promotable(project_id))
        candidates += self.store.list_jobs(project_id, status=machine.READY)
        return [job for job in candidates
                if not leases.holder(self.store, job["id"])
                and not gates.open_gates(self.store, job_id=job["id"])]

    def plan_dispatch(self, job):
        """What dispatching this job *would* do. No writes, no provider call."""
        packet = self.compiler.compile(job, repo_facts=self.repo_facts)
        reviewers = [j["id"] for j in self.store.list_jobs(project_id=job["project_id"])
                     if j.get("reviews_job_id") == job["id"]]
        landing = [j["id"] for j in self.store.list_jobs(project_id=job["project_id"])
                   if j.get("lands_job_id") == job["id"]]
        routing = self.router.route(job["role"])
        mode = self.permission_mode_for(job)
        return {
            "job_id": job["id"], "role": job["role"], "risk": job["risk"],
            "permission_mode": mode,
            "bypass_permissions": permission_policy.is_bypass(mode),
            "worktree": job["worktree"],
            "review_policy": job["review_policy"],
            "model": routing.model_id, "effort": routing.effort,
            "routing_source": routing.source, "critical_role": routing.critical,
            "session_policy": job["session_policy"],
            "session_id": job["session_id"] if job["session_policy"] == "reuse" else None,
            "provider": self.provider.name,
            "packet_sections": [title for title, _ in packet.sections],
            "packet_chars": len(packet.render()),
            "packet_sources": packet.sources,
            "intended_reviewers": reviewers,
            "intended_landing": landing,
            "open_gates": [g["kind"] for g in gates.open_gates(self.store, job_id=job["id"])],
        }

    # --- dispatch ---------------------------------------------------------

    def dispatch(self, job):
        """Lease, compile, run, and return (run, outcome). Never routes."""
        if self.dry_run:
            raise RuntimeError("dispatch() called in dry-run mode; use plan_dispatch()")
        try:
            token = leases.acquire(self.store, job["id"], self.owner, self.lease_ttl)
        except LeaseError:
            return None, None

        try:
            # Routing is locked across every arm of an experiment before either
            # runs, then the pair is checked — so no arm can be dispatched
            # against a configuration the other one did not get.
            routing = self.router.route(job["role"])
            mode = self.permission_mode_for(job, record=True)
            if job["role"] in permission_policy.PRIVILEGED_ROLES and job["lands_job_id"]:
                # A privileged job is bounded before it starts, not trusted after.
                landing.preconditions(self.store, job)
                job = self.store.get_job(job["id"])
            # Persist what this job will actually run as, then propagate it to
            # every other arm if this job is part of an experiment.
            self.store.update_job(job["id"], model=routing.model_id,
                                  effort=routing.effort, permission_mode=mode)
            experiments.lock_routing(self.store, job, routing.model_id, routing.effort,
                                     permission_mode=mode)
            experiments.check_pair(self.store, job)
            job = self.store.get_job(job["id"])
            continuation = job["metadata"].get("continuation")
            session_id = job["session_id"] if job["session_policy"] == "reuse" else None
            if continuation and session_id:
                packet = self.compiler.compile_continuation(job, continuation)
            else:
                packet = self.compiler.compile(job, repo_facts=self.repo_facts)
            self.compiler.persist(job, packet)
            prompt = packet.render() + self.provider.result_instructions(job)
            self._persist_prompt(job, prompt)

            self.store.transition(job["id"], machine.DISPATCHED, actor="scheduler",
                                  reason=f"provider={self.provider.name}")
            attempt = job["attempt"] + 1
            self.store.update_job(job["id"], attempt=attempt)
            tools = tuple(job["metadata"].get("tools")
                          or tool_policy.profile_for(job["role"]))
            tool_policy.assert_read_only(job["role"], tools)
            run = metrics.start_run(self.store, self.store.get_job(job["id"]),
                                    provider=self.provider.name, model=routing.model_id,
                                    session_id=session_id, routing=routing, tools=tools,
                                    permission_mode=mode, worktree=job["worktree"])
            self.store.transition(job["id"], machine.RUNNING, actor=_actor(job),
                                  reason=f"run {run['id']}")

            request = RunRequest(
                job_id=job["id"], role=job["role"], prompt=prompt,
                workdir=job["worktree"] or job["repo"], session_id=session_id,
                model=routing.model_id, effort=routing.effort,
                fallback_model=routing.fallback_model,
                max_budget_usd=routing.max_budget_usd, tools=tools,
                disallowed_tools=tuple(tool_policy.disallowed_for(job["role"])),
                permission_mode=mode,
                timeout_s=job["metadata"].get("timeout_s", 900), attempt=attempt,
                metadata={"risk": job["risk"], "goal_id": job["goal_id"],
                          "routing": routing.to_dict()},
            )
            before = (integrity.snapshot(job["worktree"])
                      if tool_policy.is_read_only(job["role"]) else None)
            try:
                outcome = (self.provider.resume(request) if session_id
                           else self.provider.run(request))
            except Exception as exc:                      # a provider crash is a run failure
                outcome = RunOutcome(status=RUN_FAILED, error=f"{type(exc).__name__}: {exc}")
            if before is not None:
                # An allowlist is a hope; this is the guarantee.
                after = integrity.snapshot(job["worktree"])
                integrity.assert_unchanged(job["id"], job["role"], before, after)
                left_behind = integrity.side_effects(before, after)
                if left_behind:
                    metrics.record(self.store, "run.untracked_files_left",
                                   value=left_behind, job_id=job["id"], run_id=run["id"])

            metrics.finish_run(self.store, run["id"], outcome)
            if getattr(outcome, "raw_text", None):
                # Keep the unusable answer so the next attempt can be aimed at
                # what actually went wrong rather than guessed at.
                artifacts.write(self.store, job["project_id"], job["id"],
                                f"unstructured-attempt-{job['attempt'] + 1}.md",
                                outcome.raw_text, "transcript",
                                summary=f"worker output that failed the result contract: "
                                        f"{(outcome.error or '')[:120]}")
            if outcome.session_id:
                self.store.update_job(job["id"], session_id=outcome.session_id)
            return self.store.get_job(job["id"]), outcome
        finally:
            leases.release(self.store, job["id"], token)

    def permission_mode_for(self, job, record=False):
        """Resolve the execution policy for one job, recording any downgrade."""
        mode, reason = permission_policy.resolve(
            job["role"], worktree=job["worktree"], shared_paths=self.shared_paths,
            policy=self.permission_policy)
        if reason and record:
            self.store.record_event(
                "permission.downgraded",
                {"job_id": job["id"], "role": job["role"], "mode": mode,
                 "reason": reason},
                project_id=job["project_id"], job_id=job["id"])
        return mode

    def _persist_prompt(self, job, prompt):
        directory = config.job_dir(job["project_id"], job["id"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prompt.md").write_text(prompt)

    def record_artifacts(self, job, result):
        """Register what the worker says it produced, plus its own report."""
        registered = []
        for entry in result.artifacts or []:
            registered.append(artifacts.register(
                self.store, job["project_id"], entry.get("kind", "report"),
                entry.get("uri", ""), job_id=job["id"],
                summary=entry.get("summary", ""), sha256=entry.get("sha256"),
            ))
        if result.summary:
            registered.append(artifacts.write(
                self.store, job["project_id"], job["id"], "report.md",
                _report(job, result), "report", summary=result.summary[:200]))
        return registered


def _actor(job):
    """Worker identity. Distinct per job, which is what makes independence checkable."""
    return f"worker:{job['id']}"


def _report(job, result):
    lines = [f"# {job['id']} — {job['role']}", "", result.summary or "", ""]
    if result.evidence:
        lines += ["## Evidence"] + [f"- {item}" for item in result.evidence] + [""]
    if result.blockers:
        lines += ["## Blockers"] + [f"{i}. {b}" for i, b in enumerate(result.blockers, 1)] + [""]
    if result.notes:
        lines += ["## Notes", result.notes, ""]
    return "\n".join(lines)
