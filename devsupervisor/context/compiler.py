"""The context compiler.

Builds the smallest high-signal packet a job needs, and nothing else. Its
non-goals are as load-bearing as its goals: no transcript dumps, no other
project's memory, no builder reasoning inside a reviewer packet, no held-out
data inside an implementation packet.
"""

import re
from dataclasses import dataclass, field

from .. import artifacts as artifact_registry
from .. import config
from ..memory import redact
from ..memory.store import MemoryStore
from ..policy import immutable

# A packet that grows without bound stops being a packet.
DEFAULT_MEMORY_LIMIT = 6
DEFAULT_MEMORY_CHARS = 6000

# Roles that must not inherit the builder's reasoning; that inheritance is
# exactly what would make the review non-independent.
INDEPENDENT_ROLES = frozenset({"reviewer", "qa", "security", "evaluator"})

_WORD = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class JobPacket:
    job_id: str
    role: str
    sections: list = field(default_factory=list)   # [(title, body)]
    sources: list = field(default_factory=list)    # cited memory/artifact paths

    def render(self):
        blocks = [f"# Job packet — {self.job_id} ({self.role})", ""]
        for title, body in self.sections:
            blocks.append(f"## {title}")
            blocks.append(body.rstrip())
            blocks.append("")
        return "\n".join(blocks).rstrip() + "\n"

    def text(self):
        return self.render()

    def to_dict(self):
        return {"job_id": self.job_id, "role": self.role,
                "sections": [{"title": t, "body": b} for t, b in self.sections],
                "sources": list(self.sources)}


class ContextCompiler:
    """Assembles job packets. Deterministic given the same state."""

    def __init__(self, store, memory_limit=DEFAULT_MEMORY_LIMIT,
                 memory_chars=DEFAULT_MEMORY_CHARS, policy_pack=None):
        self.store = store
        self.memory_limit = memory_limit
        self.memory_chars = memory_chars
        self.policy_pack = policy_pack

    # --- public -----------------------------------------------------------

    def compile(self, job, goal=None, repo_facts=None, review_findings=None):
        project = self.store.require_project(job["project_id"])
        goal = goal or (self.store.get_goal(job["goal_id"]) if job["goal_id"] else None)
        packet = JobPacket(job_id=job["id"], role=job["role"])

        packet.sections.append(("Immutable safety policy", immutable.render()))
        packet.sections.append(("Project policy", self._project_policy(project)))
        if goal:
            packet.sections.append(("Goal", self._goal(goal)))
        packet.sections.append(("Job contract", self._contract(job)))

        dependency_block, dependency_sources = self._dependency_outputs(job)
        if dependency_block:
            packet.sections.append(("Dependency outputs", dependency_block))
            packet.sources.extend(dependency_sources)

        if job["role"] in INDEPENDENT_ROLES:
            candidate = self._candidate_under_review(job)
            if candidate:
                packet.sections.append(("Candidate under review", candidate))

        memory_block, memory_sources = self._memory(job, goal, project)
        if memory_block:
            packet.sections.append(("Relevant project memory", memory_block))
            packet.sources.extend(memory_sources)

        if repo_facts:
            packet.sections.append(("Verified repository facts", _render_facts(repo_facts)))

        findings = review_findings if review_findings is not None else job.get("blockers")
        if findings and job.get("revision_of"):
            packet.sections.append(("Reviewer blockers to fix — verbatim",
                                    _render_blockers(findings)))

        packet.sections = [(title, redact.redact(body)) for title, body in packet.sections]
        return packet

    def compile_continuation(self, job, instruction, goal=None):
        """A short follow-up for a session that already holds this job's context.

        Re-sending the full packet to a live session pays for context it already
        has and invites it to start over. What a continuation needs is the
        safety rules, which are never optional, what changed, and the specific
        ask.
        """
        packet = JobPacket(job_id=job["id"], role=job["role"])
        packet.sections.append(("Immutable safety policy", immutable.render()))
        packet.sections.append((
            "Continuing your earlier session",
            f"You are continuing work on {job['id']} ({job['role']}). Your earlier "
            f"context still applies; do not start over.\n\n"
            f"OUT OF SCOPE (unchanged): {job.get('non_goals') or '(unspecified)'}\n\n"
            f"OUTPUT CONTRACT (unchanged): {job.get('output_contract') or ''}"))
        packet.sections.append(("What to do now", instruction))
        packet.sections = [(title, redact.redact(body)) for title, body in packet.sections]
        return packet

    def persist(self, job, packet):
        """Freeze the packet next to the job so the handoff outlives the process."""
        directory = config.job_dir(job["project_id"], job["id"])
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "context.md"
        path.write_text(packet.render())
        return path

    # --- sections ---------------------------------------------------------

    def _project_policy(self, project):
        lines = [f"Project: {project['name']} ({project['id']})",
                 f"Repository: {project['repo_path']}"]
        pack = self.policy_pack
        if pack is not None:
            lines.append("")
            lines.append(pack.render())
        elif project.get("policy_pack"):
            lines.append(f"Policy pack: {project['policy_pack']} (not loaded in this packet)")
        return "\n".join(lines)

    def _goal(self, goal):
        lines = [goal["title"]]
        if goal.get("description"):
            lines += ["", goal["description"]]
        if goal.get("acceptance_criteria"):
            lines += ["", "Goal acceptance criteria:"]
            lines += [f"- {c}" for c in goal["acceptance_criteria"]]
        return "\n".join(lines)

    def _contract(self, job):
        lines = [
            f"Job: {job['id']}",
            f"Type/role: {job['job_type']} / {job['role']}",
            f"Risk: {job['risk']}    Review policy: {job['review_policy']}",
        ]
        for label, key in (("Branch", "branch"), ("Base SHA", "base_sha"),
                           ("Worktree", "worktree")):
            if job.get(key):
                lines.append(f"{label}: {job[key]}")
        lines += ["", "WILL DO:", job.get("scope") or "(unspecified)"]
        lines += ["", "OUT OF SCOPE:", job.get("non_goals") or "(unspecified)"]
        criteria = job.get("acceptance_criteria") or []
        lines += ["", "DONE MEANS:"]
        lines += [f"- {c}" for c in criteria] or ["- (unspecified)"]
        lines += ["", "OUTPUT CONTRACT:", job.get("output_contract") or
                  "Write a structured result: status, summary, artifacts, evidence."]
        return "\n".join(lines)

    def _dependency_outputs(self, job):
        lines, sources = [], []
        for dependency_id in self.store.dependencies(job["id"]):
            dependency = self.store.get_job(dependency_id)
            if dependency is None:
                continue
            lines.append(f"- {dependency_id} [{dependency['status']}] {dependency['scope'] or ''}".rstrip())
            for artifact in artifact_registry.for_job(self.store, dependency_id):
                if artifact["kind"] == "transcript":
                    continue           # references only; never the conversation
                lines.append(f"    {artifact_registry.reference(artifact)}")
                sources.append(artifact["uri"])
        return ("\n".join(lines), sources) if lines else ("", [])

    def _candidate_under_review(self, job):
        target_id = job.get("reviews_job_id")
        if not target_id:
            return ""
        target = self.store.get_job(target_id)
        if target is None:
            return ""
        lines = [f"Reviewing job: {target_id}",
                 f"Candidate SHA: {target.get('result_sha') or '(none recorded)'}",
                 f"Branch: {target.get('branch') or '(none)'}",
                 f"Base: {target.get('base_sha') or '(none)'}",
                 "",
                 "Contract the candidate was built against:",
                 f"  scope: {target.get('scope') or '(unspecified)'}",
                 f"  non-goals: {target.get('non_goals') or '(unspecified)'}"]
        criteria = target.get("acceptance_criteria") or []
        if criteria:
            lines.append("  done means:")
            lines += [f"    - {c}" for c in criteria]
        outputs = [a for a in artifact_registry.for_job(self.store, target_id)
                   if a["kind"] != "transcript"]
        if outputs:
            lines += ["", "Artifacts to inspect:"]
            lines += [f"  {artifact_registry.reference(a)}" for a in outputs]
        lines += ["", "Return APPROVE, or REJECT with numbered blockers. "
                  "Do not fix the code you are reviewing."]
        return "\n".join(lines)

    def _memory(self, job, goal, project):
        memory = MemoryStore(project["id"])   # project-scoped by construction
        terms = _query_terms(job, goal)
        documents = memory.search(terms, limit=self.memory_limit)
        if not documents:
            return "", []
        lines, sources, budget = [], [], self.memory_chars
        for document in documents:
            body = document["body"].strip()
            if len(body) > budget:
                body = body[:max(budget, 0)].rstrip() + "\n… (truncated)"
            budget -= len(body)
            lines.append(f"### {document['title']}  ({document['relative_path']})")
            lines.append(body)
            lines.append("")
            sources.append(document["relative_path"])
            if budget <= 0:
                break
        return "\n".join(lines).rstrip(), sources


def _query_terms(job, goal):
    parts = [job.get("scope") or "", job.get("non_goals") or "", job["id"], job["job_type"]]
    parts += [str(c) for c in (job.get("acceptance_criteria") or [])]
    if goal:
        parts += [goal["title"], goal.get("description") or ""]
    return [word for part in parts for word in _WORD.findall(part)]


def _render_facts(facts):
    return "\n".join(f"- {key}: {value}" for key, value in facts.items())


def _render_blockers(blockers):
    lines = ["The reviewer rejected the previous attempt. Fix exactly these, nothing else.", ""]
    lines += [f"{index}. {blocker}" for index, blocker in enumerate(blockers, start=1)]
    return "\n".join(lines)
