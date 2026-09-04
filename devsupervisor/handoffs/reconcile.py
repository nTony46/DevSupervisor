"""Reconcile handoffs into logical jobs.

Identity is `(repo, branch, base_sha, result_sha, task)` — never an agent
number. Agent labels do not survive a restart: in the corpus this was built
from, three separate sessions all called themselves "Agent 6", and one file is
filed under a name that describes a different project entirely.

Claims are verified against git where a repository is available. A handoff that
disagrees with the repository is recorded as a disagreement, not corrected
silently and not believed.
"""

from dataclasses import dataclass, field

from .. import gitfacts, ids

# Handoff status vocabulary mapped to what the supervisor should do next.
CLASSIFICATIONS = (
    "COMPLETE", "WAITING_REVIEW", "WAITING_HUMAN", "ACTIVE", "BLOCKED",
    "PAUSED", "SUPERSEDED", "DUPLICATE", "UNVERIFIED", "UNKNOWN",
)


@dataclass
class LogicalJob:
    key: str
    title: str
    branch: str = None
    head: str = None
    base: str = None
    repo: str = None
    classification: str = "UNKNOWN"
    sources: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    verified: dict = field(default_factory=dict)
    disagreements: list = field(default_factory=list)
    claimed_status: str = "UNKNOWN"
    landed_as: str = None

    def to_dict(self):
        return dict(self.__dict__)


def logical_key(handoff):
    """What makes two handoffs the same piece of work."""
    if handoff.branch:
        return f"branch:{handoff.branch}"
    return f"task:{ids.slug(handoff.title or handoff.name, max_words=6)}"


def reconcile(handoffs, repo=None, main_ref="origin/main"):
    """Group handoffs into logical jobs and verify their claims against git.

    Branch-inventory rows are treated as first-class work. An agent's own header
    fields name only the lane it was on at the moment it stopped; the inventory
    table is where the rest of its branches are listed, and some of those are
    the ones still open.
    """
    groups = {}
    for handoff in handoffs:
        groups.setdefault(logical_key(handoff), []).append(handoff)
        for entry in handoff.inventory:
            groups.setdefault(f"branch:{entry['branch']}", []).append(
                _inventory_handoff(handoff, entry))

    jobs = []
    for key, members in sorted(groups.items()):
        authoritative = max(members, key=_evidence_score)
        job = LogicalJob(
            key=key,
            title=authoritative.title or authoritative.name,
            branch=authoritative.branch,
            head=authoritative.head,
            base=authoritative.base,
            repo=repo or authoritative.repo,
            claimed_status=authoritative.status,
            landed_as=getattr(authoritative, "landed_as", None),
            sources=sorted({m.name for m in members}),
            duplicates=sorted({m.name for m in members if m is not authoritative}
                              - {authoritative.name}),
        )
        if repo:
            # Check every member's claims, not just the authoritative one: a
            # false claim in a secondary handoff is exactly the thing worth
            # surfacing, and it is invisible if only the best source is checked.
            _verify(job, members, repo, main_ref)
        job.classification = classify(job)
        jobs.append(job)
    return jobs


def _inventory_handoff(source, entry):
    """A synthetic handoff for one row of a branch-inventory table."""
    from .parser import Handoff
    return Handoff(
        path=source.path, name=source.name,
        title=f"{entry['branch']} (from {source.name} inventory)",
        status=source.status, branch=entry["branch"], head=entry["head"],
        base=source.base, repo=source.repo, merged=entry["merged"],
        body=entry["state_text"], status_text=entry["state_text"],
        landed_as=entry["landed_as"],
    )


def _evidence_score(handoff):
    """Prefer the handoff that states checkable facts over the one that asserts."""
    return sum(1 for value in (handoff.branch, handoff.head, handoff.base,
                               handoff.repo, handoff.worktree) if value)


def _verify(job, members, repo, main_ref):
    """Check the claims. Record what git says, and where each source disagrees."""
    if not gitfacts.is_repo(repo):
        job.verified = {"repo": False}
        return
    verified = {"repo": True}
    if job.branch:
        branch_head = gitfacts.rev_parse(repo, job.branch)
        verified["branch_exists"] = branch_head is not None
        verified["branch_head"] = branch_head
        if job.head and branch_head and not branch_head.startswith(job.head):
            job.disagreements.append(
                f"handoff claims HEAD {job.head} for {job.branch}, git says {branch_head[:12]}")
            job.head = branch_head
        elif branch_head and not job.head:
            job.head = branch_head
    if job.head:
        verified["commit_exists"] = gitfacts.commit_exists(repo, job.head)
        if verified["commit_exists"]:
            verified["merged"] = gitfacts.is_ancestor(repo, job.head, main_ref)
        else:
            job.disagreements.append(
                f"commit {job.head[:12]} is not present in {repo}; "
                f"this work probably belongs to a different repository")
    if job.head and verified.get("commit_exists") and verified.get("merged") is False:
        # Unmerged by SHA is not the same as pending. Check whether the same
        # content already landed under a different commit before proposing work.
        content = gitfacts.landed_by_content(repo, job.head, main_ref,
                                             against=job.landed_as)
        verified["landed_by_content"] = content.get("landed_by_content")
        verified["content_compared_against"] = content.get("compared_against")
    for member in members:
        if member.merged is None or verified.get("merged") is None:
            continue
        if member.merged != verified["merged"]:
            job.disagreements.append(
                f"{member.name} claims merged={member.merged}, "
                f"git says merged={verified['merged']}")
    job.verified = verified


def classify(job):
    """What state this work is actually in, preferring git over prose."""
    if job.verified.get("merged") is True:
        return "COMPLETE"
    if job.verified.get("landed_by_content") is True:
        # The work is in main under another SHA. Re-landing it would be a defect.
        return "SUPERSEDED"
    if not job.branch and not job.head:
        # A claim with no branch and no SHA cannot be checked. Saying
        # "waiting for review" would assert something we do not know.
        return "UNVERIFIED"
    claimed = job.claimed_status
    if job.verified.get("commit_exists") is False:
        return "UNKNOWN"
    if claimed == "COMPLETE":
        # Finished but unmerged is the single most common real state: it needs a
        # review and a landing decision, not a rebuild.
        return "WAITING_REVIEW"
    if claimed == "WAITING":
        return "WAITING_REVIEW"
    if claimed in ("ACTIVE", "INTERRUPTED"):
        return "ACTIVE"
    if claimed == "PARKED":
        return "PAUSED"
    if claimed == "BLOCKED":
        return "BLOCKED"
    return "UNKNOWN"


def summarize(jobs):
    counts = {}
    for job in jobs:
        counts[job.classification] = counts.get(job.classification, 0) + 1
    return {
        "logical_jobs": len(jobs),
        "by_classification": dict(sorted(counts.items())),
        "with_duplicates": [j.key for j in jobs if j.duplicates],
        "with_disagreements": [j.key for j in jobs if j.disagreements],
    }
