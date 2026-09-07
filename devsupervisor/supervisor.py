"""The outer loop: what a result means, and what happens next.

Observe state, select ready jobs, delegate, collect, route to review, revise or
land, verify, check the goal, update memory and metrics, repeat. Every decision
here is made from the database, never from conversation history.
"""

from . import delegation, gates, landing, metrics
from .context import ContextCompiler
from .errors import TransitionGuardFailed
from .memory import candidates as memory_candidates
from .planner import Planner
from .policy.packs import PolicyPack
from .policy.routing import ModelRouter
from .providers import MockProvider
from .results import InvalidResult, WorkerResult
from .scheduler import Scheduler, _actor
from .state import leases, machine

REVIEW_ROLES = frozenset({"reviewer", "specialist", "qa", "security"})
# Landing merges an approved candidate; freezing records one as authoritative
# without touching the repository. Both close out a candidate's lifecycle.
CLOSING_ROLES = frozenset({"landing", "freeze"})
VERDICT_ROLES = REVIEW_ROLES | {"evaluator"}


class Supervisor:
    def __init__(self, store, provider=None, pack=None, owner="supervisor-1",
                 dry_run=False, repo_facts=None, concurrency=1, router=None,
                 shared_paths=None, permission_policy_body=None):
        self.store = store
        self.pack = pack or PolicyPack()
        self.provider = provider or MockProvider()
        self.router = router or ModelRouter(store, provider_name=self.provider.name)
        self.compiler = ContextCompiler(store, policy_pack=self.pack)
        self.dry_run = dry_run
        self.owner = owner
        self.scheduler = Scheduler(
            store, self.provider, compiler=self.compiler, owner=owner,
            concurrency=concurrency, repo_facts=repo_facts, dry_run=dry_run,
            router=self.router, permission_policy_body=permission_policy_body,
            # Every registered project's own checkout is shared state. A bypassed
            # worker pointed at one of them is a worker in the wrong place.
            shared_paths=(shared_paths if shared_paths is not None
                          else tuple(p["repo_path"] for p in store.list_projects())),
        )

    # --- planning ---------------------------------------------------------

    def plan_goal(self, goal, **kwargs):
        return Planner(self.store, pack=self.pack).plan(goal, **kwargs)

    # --- the loop ---------------------------------------------------------

    def run(self, project_id=None, max_iterations=200):
        """Drive until nothing is ready. Returns a summary of what happened."""
        lock_owner = leases.acquire_supervisor_lock(self.store, self.owner)
        try:
            dispatched, iterations = [], 0
            while iterations < max_iterations:
                iterations += 1
                jobs = self.scheduler.ready_jobs(project_id, limit=self.scheduler.concurrency)
                if not jobs:
                    break
                for job in jobs:
                    advanced = self.advance(job)
                    if advanced:
                        dispatched.append(advanced)
            return {
                "iterations": iterations,
                "dispatched": dispatched,
                "open_gates": gates.open_gates(self.store, project_id=project_id),
                "stopped_because": self._stop_reason(project_id),
            }
        finally:
            leases.release_supervisor_lock(self.store, lock_owner)

    def advance(self, job):
        """Dispatch one job and route its result. Returns the job id, or None."""
        job, outcome = self.scheduler.dispatch(job)
        if job is None:
            return None
        self.ingest(job, outcome)
        return job["id"]

    def _stop_reason(self, project_id):
        if gates.open_gates(self.store, project_id=project_id):
            return "waiting on a human gate"
        if self.store.list_jobs(project_id, status=machine.BLOCKED):
            return "blocked jobs need attention"
        remaining = self.store.list_jobs(
            project_id, status=[machine.PLANNED, machine.READY, machine.PAUSED])
        return "no ready jobs" if remaining else "all jobs complete"

    # --- ingestion --------------------------------------------------------

    def ingest(self, job, outcome):
        """Apply a worker's structured result. Unstructured output is a failure."""
        if not outcome.succeeded or outcome.result is None:
            return self._fail(job, outcome.error or f"run status {outcome.status}")
        try:
            result = outcome.result.validate(role=job["role"])
        except InvalidResult as exc:
            return self._fail(job, f"invalid result: {exc}")

        self.scheduler.record_artifacts(job, result)
        self._record_memory_candidates(job, result)
        self._record_metrics(job, result)
        # A worker may ask for help. Only the supervisor may create the job.
        delegation.authorize(self.store, job, result.subtask_requests, pack=self.pack)

        if result.status in ("BLOCKED", "NEEDS_HUMAN"):
            return self._park(job, result)

        fields = {"result_sha": result.result_sha} if result.result_sha else None
        job = self.store.transition(job["id"], machine.WORK_COMPLETE, actor=_actor(job),
                                    reason=result.summary[:200], fields=fields)
        self.store.record_event("result.ingested", {"job_id": job["id"],
                                                    "status": result.status},
                                job_id=job["id"], project_id=job["project_id"],
                                idempotency_key=f"{job['id']}:attempt:{job['attempt']}")
        return self.route(job, result)

    def _fail(self, job, reason):
        target = machine.FAILED if job["attempt"] >= job["max_attempts"] else machine.READY
        self.store.transition(job["id"], target, actor="supervisor", reason=reason[:400])
        metrics.record(self.store, "job.failure", text=reason[:200], job_id=job["id"])
        if target == machine.FAILED:
            gates.open_gate(
                self.store, "retries_exhausted",
                f"{job['id']} failed {job['attempt']} times: {reason[:200]}",
                project_id=job["project_id"], job_id=job["id"],
                resume_status=machine.READY)
        return None

    def _park(self, job, result):
        reason = result.summary or "worker reported it is blocked"
        if result.status == "NEEDS_HUMAN":
            self.store.transition(job["id"], machine.WORK_COMPLETE, actor=_actor(job),
                                  reason=reason[:200])
            gates.open_gate(self.store, "conflicting_evidence", reason[:400],
                            project_id=job["project_id"], job_id=job["id"],
                            context="\n".join(result.blockers or []),
                            resume_status=machine.READY)
        else:
            self.store.transition(job["id"], machine.BLOCKED, actor=_actor(job),
                                  reason=reason[:400])
        return None

    def _record_memory_candidates(self, job, result):
        for candidate in result.memory_candidates or []:
            memory_candidates.propose(
                job["project_id"], candidate.get("title", f"note from {job['id']}"),
                candidate.get("body", ""), area=candidate.get("area", "lessons"),
                tags=candidate.get("tags", ()), source_job=job["id"])

    def _record_metrics(self, job, result):
        for name, value in (result.metrics or {}).items():
            if isinstance(value, (int, float)):
                metrics.record(self.store, f"worker.{name}", value=value, job_id=job["id"])
            else:
                metrics.record(self.store, f"worker.{name}", text=str(value), job_id=job["id"])
        if job["role"] in VERDICT_ROLES and result.verdict:
            metrics.record(self.store, "review.verdict", text=result.verdict, job_id=job["id"])
            for blocker in result.blockers or []:
                metrics.record(self.store, "review.blocker", text=blocker[:200], job_id=job["id"])

    # --- routing ----------------------------------------------------------

    def route(self, job, result):
        if job["role"] in REVIEW_ROLES and job["reviews_job_id"]:
            return self._apply_review(job, result)
        if job["role"] in CLOSING_ROLES and job["lands_job_id"]:
            return self._apply_landing(job, result)
        if job["role"] == "evaluator" and job["reviews_job_id"]:
            return self._apply_evaluation(job, result)
        return self._route_candidate(job)

    def _route_candidate(self, job):
        """Route work that produced something.

        Review policy decides this, not role. Keying on role alone meant a
        producing role other than "build" — a benchmark author, say — walked
        into APPROVED with an independent review policy still set, and the guard
        correctly refused. Anything that owes a review goes to UNDER_REVIEW;
        only work that is actually landable proceeds toward landing.
        """
        if job["review_policy"] != "none":
            return self.store.transition(job["id"], machine.UNDER_REVIEW,
                                         actor="supervisor",
                                         reason="awaiting independent review")
        if job["role"] in machine.LANDABLE_ROLES:
            self.store.transition(job["id"], machine.APPROVED, actor="supervisor",
                                  reason="review_policy=none")
            return self.store.transition(job["id"], machine.LANDING_READY,
                                         actor="supervisor",
                                         reason="no review required")
        return self._finish(job)

    def _finish(self, job):
        """Close a job that has nothing to land."""
        self.store.transition(job["id"], machine.APPROVED, actor="supervisor",
                              reason="work accepted")
        return self.store.transition(job["id"], machine.DONE, actor="supervisor",
                                     reason="nothing to land")

    def _apply_review(self, reviewer_job, result):
        target = self.store.require_job(reviewer_job["reviews_job_id"])
        if result.verdict == "APPROVE":
            self.store.transition(target["id"], machine.APPROVED,
                                  actor=_actor(reviewer_job),
                                  reason=f"approved by {reviewer_job['id']}")
            self.store.transition(target["id"], machine.LANDING_READY, actor="supervisor",
                                  reason="approved candidate ready to land")
            return self._finish(reviewer_job)

        self.store.transition(target["id"], machine.REJECTED, actor=_actor(reviewer_job),
                              reason=f"rejected by {reviewer_job['id']}",
                              fields={"blockers": result.blockers})
        self._finish(reviewer_job)
        return self.revise(target, result.blockers)

    def _apply_landing(self, landing_job, result):
        """Close out an approved candidate — by landing it, or by freezing it.

        The worker's report is not the evidence. For a landing, git is: the
        approved SHA must now be in the target, the target's previous tip must
        still be an ancestor of it, and the patch must be the reviewed one.
        """
        target = self.store.require_job(landing_job["lands_job_id"])
        verb = "frozen" if landing_job["role"] == "freeze" else "landed"
        if landing_job["role"] == "landing":
            findings = landing.verify(self.store, landing_job, result.result_sha)
            metrics.record(self.store, "landing.verified",
                           value=1 if findings.get("checked") else 0,
                           text=findings.get("reason"), job_id=landing_job["id"])
        if target["status"] == machine.LANDING_READY:
            self.store.transition(target["id"], machine.LANDING, actor=_actor(landing_job),
                                  reason=f"{verb} by {landing_job['id']}")
        self.store.transition(
            target["id"], machine.VERIFIED, actor=_actor(landing_job),
            reason=result.summary[:200] or f"{verb} and verified",
            fields={"metadata": dict(target["metadata"],
                                     **{f"{verb}_sha": result.result_sha or
                                        target["result_sha"]})})
        return self._finish(landing_job)

    def _apply_evaluation(self, evaluator_job, result):
        target = self.store.require_job(evaluator_job["reviews_job_id"])
        self.store.transition(target["id"], machine.EVALUATED, actor=_actor(evaluator_job),
                              reason=f"evaluated by {evaluator_job['id']}")
        if result.verdict == "APPROVE":
            self.store.transition(target["id"], machine.DONE, actor=_actor(evaluator_job),
                                  reason="goal criteria satisfied")
            finished = self._finish(evaluator_job)
            self._close_goal_if_complete(target)
            return finished

        self.store.transition(target["id"], machine.REJECTED, actor=_actor(evaluator_job),
                              reason="goal criteria not satisfied",
                              fields={"blockers": result.blockers})
        self._finish(evaluator_job)
        return self.revise(target, result.blockers)

    def _close_goal_if_complete(self, job):
        if not job["goal_id"]:
            return
        outstanding = [j for j in self.store.list_jobs(goal_id=job["goal_id"])
                       if j["status"] not in machine.TERMINAL]
        if not outstanding:
            self.store.set_goal_status(job["goal_id"], "DONE")

    # --- targeted revision ------------------------------------------------

    def revise(self, build_job, blockers):
        """A rejection creates a new candidate job carrying exactly the blockers.

        The rejected attempt is superseded, not deleted, and everything that
        depended on it is repointed at the new attempt — including a fresh
        review job, because a spent reviewer cannot re-review.
        """
        try:
            self.store.transition(
                build_job["id"], machine.REVISION_READY, actor="supervisor",
                reason="targeted revision", fields={
                    "revision_count": build_job["revision_count"] + 1,
                    "blockers": list(blockers or []),
                })
        except TransitionGuardFailed as exc:
            self.store.transition(build_job["id"], machine.BLOCKED, actor="supervisor",
                                  reason=str(exc)[:400])
            gates.open_gate(
                self.store, "retries_exhausted", str(exc)[:400],
                project_id=build_job["project_id"], goal_id=build_job["goal_id"],
                job_id=build_job["id"], context="\n".join(blockers or []),
                resume_status=machine.PLANNED)
            return None

        revision = self._clone_candidate(build_job, blockers)
        self._rewire(build_job, revision)
        self.store.transition(build_job["id"], machine.SUPERSEDED, actor="supervisor",
                              reason=f"superseded by {revision['id']}")
        self.store.relate(build_job["id"], revision["id"], "SUPERSEDED_BY",
                          note="targeted revision")
        self.store.relate(revision["id"], build_job["id"], "SUPERSEDES")
        return revision

    def _clone_candidate(self, build_job, blockers):
        return self.store.create_job(
            build_job["project_id"], build_job["job_type"], build_job["role"],
            _subject(build_job), goal_id=build_job["goal_id"], plan_id=build_job["plan_id"],
            depends_on=[(edge["depends_on"], edge["satisfied_by"])
                        for edge in self.store.dependency_edges(build_job["id"])],
            risk=build_job["risk"], review_policy=build_job["review_policy"],
            scope=build_job["scope"], non_goals=build_job["non_goals"],
            acceptance_criteria=build_job["acceptance_criteria"],
            output_contract=build_job["output_contract"],
            repo=build_job["repo"], branch=build_job["branch"],
            base_sha=build_job["base_sha"], worktree=build_job["worktree"],
            revision_of=build_job["id"], blockers=list(blockers or []),
            revision_count=build_job["revision_count"] + 1,
            max_revisions=build_job["max_revisions"],
            session_id=build_job["session_id"], session_policy=build_job["session_policy"],
            metadata=dict(build_job["metadata"], step="build"),
        )

    def _rewire(self, old, new):
        """Move dependents onto the new attempt; replace spent reviewers."""
        spent_reviewers = []
        for dependent_id in self.store.dependents(old["id"]):
            dependent = self.store.get_job(dependent_id)
            if dependent is None:
                continue
            if dependent["role"] in REVIEW_ROLES and dependent["reviews_job_id"] == old["id"]:
                spent_reviewers.append(dependent)
                continue
            threshold = next(
                (e["satisfied_by"] for e in self.store.dependency_edges(dependent_id)
                 if e["depends_on"] == old["id"]), machine.DONE)
            self.store.remove_dependency(dependent_id, old["id"])
            self.store.add_dependency(dependent_id, new["id"], satisfied_by=threshold)

        for job in self.store.list_jobs(project_id=old["project_id"]):
            if job["reviews_job_id"] == old["id"] and job["role"] not in REVIEW_ROLES:
                self.store.update_job(job["id"], reviews_job_id=new["id"])
            if job["lands_job_id"] == old["id"]:
                self.store.update_job(job["id"], lands_job_id=new["id"])

        for reviewer in spent_reviewers:
            self._replace_reviewer(reviewer, new)

    def _replace_reviewer(self, spent, new_candidate):
        replacement = self.store.create_job(
            spent["project_id"], spent["job_type"], spent["role"], _subject(spent),
            goal_id=spent["goal_id"], plan_id=spent["plan_id"],
            depends_on=[(new_candidate["id"], machine.UNDER_REVIEW)],
            risk=spent["risk"], review_policy="none", scope=spent["scope"],
            non_goals=spent["non_goals"], acceptance_criteria=spent["acceptance_criteria"],
            output_contract=spent["output_contract"], repo=spent["repo"],
            branch=spent["branch"], base_sha=spent["base_sha"], worktree=spent["worktree"],
            reviews_job_id=new_candidate["id"], session_policy="fresh",
            metadata=dict(spent["metadata"], replaces=spent["id"]),
        )
        for dependent_id in self.store.dependents(spent["id"]):
            threshold = next(
                (e["satisfied_by"] for e in self.store.dependency_edges(dependent_id)
                 if e["depends_on"] == spent["id"]), machine.DONE)
            self.store.remove_dependency(dependent_id, spent["id"])
            self.store.add_dependency(dependent_id, replacement["id"], satisfied_by=threshold)
        return replacement

    # --- dry run ----------------------------------------------------------

    def fan_out(self, parent_job, specs, actor="supervisor"):
        """Supervisor-initiated parallel children: experiment arms, independent
        implementation lanes, research, extra reviewers, specialist QA."""
        return delegation.fan_out(self.store, parent_job, specs, pack=self.pack,
                                  actor=actor)

    def dry_run_report(self, project_id=None):
        """What the scheduler would do next, with no writes and no provider calls."""
        ready = self.scheduler.candidate_jobs(project_id)
        return {
            "routing": [d.to_dict() for d in self.router.table()],
            "ready": [self.scheduler.plan_dispatch(job) for job in ready],
            "waiting_human": [
                {"job_id": job["id"], "role": job["role"],
                 "gates": [g["kind"] for g in gates.open_gates(self.store, job_id=job["id"])]}
                for job in self.store.list_jobs(project_id, status=machine.WAITING_HUMAN)
            ],
            "blocked": [job["id"] for job in
                        self.store.list_jobs(project_id, status=machine.BLOCKED)],
            "open_gates": gates.open_gates(self.store, project_id=project_id),
        }


def _subject(job):
    """Recover the human-readable subject from a stable job id."""
    parts = job["id"].split("-")
    return " ".join(parts[1:-1]) or job["id"]
