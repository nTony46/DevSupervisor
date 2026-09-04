"""Retrospectives: observations from run history, and candidate policy changes.

Everything this module produces is a *candidate*. It cannot adopt a policy, and
it cannot reach an immutable safety rule — `learnable.propose` refuses those
names, so the guardrail is enforced by the policy layer rather than by this
module's good behaviour.
"""

from statistics import mean

from . import metrics, taxonomy
from .memory import candidates as memory_candidates
from .policy import learnable
from .state import db, machine

# Thresholds for proposing a change. Small samples propose nothing: a policy
# derived from two data points is noise with a version number.
MIN_SAMPLES = 4
HIGH_REJECTION_RATE = 0.4
LARGE_JOB_MODULES = 3
REVIEW_VALUE_SAMPLES = 5


# --- observations ---------------------------------------------------------


def observations(store, project_id=None):
    builds = store.list_jobs(project_id, role="build")
    reviewed = [job for job in builds if job["review_policy"] != "none"]
    rejected = [job for job in reviewed if _was_rejected(store, job)]

    return {
        "jobs": len(store.list_jobs(project_id)),
        "builds": len(builds),
        "reviewed_builds": len(reviewed),
        "rejected_builds": len(rejected),
        "first_pass_review_rate": _rate(len(reviewed) - len(rejected), len(reviewed)),
        "blocker_categories": _blocker_categories(store),
        "failure_categories": _failure_categories(store),
        "size_vs_rejection": _size_vs_rejection(store, reviewed, rejected),
        "review_value_by_group": _review_value(store, reviewed, rejected),
        "runs": _run_stats(store, project_id),
        "cost_usd": metrics.total_cost(store, project_id),
    }


def _rate(numerator, denominator):
    return round(numerator / denominator, 3) if denominator else None


def _was_rejected(store, job):
    return any(t["to_status"] == machine.REJECTED for t in store.transitions(job["id"]))


def _blocker_categories(store):
    counts = {}
    for row in metrics.values(store, "review.blocker"):
        category = taxonomy.classify_blocker(row["text_value"])
        counts[category] = counts.get(category, 0) + 1
    return counts


def _failure_categories(store):
    counts = {}
    for row in metrics.values(store, "job.failure"):
        category = taxonomy.classify_failure(row["text_value"])
        counts[category] = counts.get(category, 0) + 1
    return counts


def _modules_touched(store, job_id):
    rows = [r["value"] for r in metrics.values(store, "worker.modules_touched")
            if r["job_id"] == job_id and r["value"] is not None]
    return rows[0] if rows else None


def _size_vs_rejection(store, reviewed, rejected):
    rejected_ids = {job["id"] for job in rejected}
    sizes = {"rejected": [], "accepted": []}
    for job in reviewed:
        size = _modules_touched(store, job["id"])
        if size is None:
            continue
        sizes["rejected" if job["id"] in rejected_ids else "accepted"].append(size)
    return {key: {"n": len(values), "mean": round(mean(values), 2) if values else None}
            for key, values in sizes.items()}


def _group(job):
    return f"{job['job_type']}:{job['risk']}"


def _review_value(store, reviewed, rejected):
    """Where independent review has never found anything, say so with numbers."""
    rejected_ids = {job["id"] for job in rejected}
    groups = {}
    for job in reviewed:
        entry = groups.setdefault(_group(job), {"reviews": 0, "rejections": 0})
        entry["reviews"] += 1
        if job["id"] in rejected_ids:
            entry["rejections"] += 1
    return groups


def _run_stats(store, project_id=None):
    sql = ("SELECT r.status, COUNT(*) AS n FROM runs r JOIN jobs j ON j.id = r.job_id"
           " WHERE 1=1")
    params = []
    if project_id:
        sql += " AND j.project_id = ?"
        params.append(project_id)
    rows = db.all_rows(store.conn, sql + " GROUP BY r.status", params)
    return {row["status"]: row["n"] for row in rows}


# --- candidate generation -------------------------------------------------


def retrospect(store, project_id=None, actor="retrospective"):
    """Observe, then propose. Returns {'observations', 'candidates', 'memory'}."""
    facts = observations(store, project_id)
    proposals = []
    proposals += _decomposition_candidate(store, facts)
    proposals += _review_depth_candidates(store, facts)
    proposals += _retry_candidate(store, facts)

    memory_path = None
    if project_id and (proposals or facts["builds"]):
        memory_path = memory_candidates.propose(
            project_id,
            title=f"Retrospective over {facts['jobs']} jobs",
            body=render(facts, proposals),
            area="orchestration", tags=["retrospective", "delegation"], kind="retrospective")

    return {"observations": facts, "candidates": proposals, "memory_candidate": memory_path}


def _decomposition_candidate(store, facts):
    reviewed = facts["reviewed_builds"]
    if reviewed < MIN_SAMPLES:
        return []
    rejection_rate = 1 - (facts["first_pass_review_rate"] or 1)
    sizes = facts["size_vs_rejection"]
    rejected_mean = sizes["rejected"]["mean"]
    accepted_mean = sizes["accepted"]["mean"]
    if rejection_rate < HIGH_REJECTION_RATE or rejected_mean is None:
        return []
    if rejected_mean < LARGE_JOB_MODULES:
        return []
    if accepted_mean is not None and rejected_mean <= accepted_mean:
        return []

    limit = int(accepted_mean) if accepted_mean else LARGE_JOB_MODULES - 1
    return [learnable.propose(
        store, "decomposition.max_modules", "decomposition",
        {"max_modules": max(limit, 1)},
        rationale=(
            f"{round(rejection_rate * 100)}% of reviewed builds were rejected. "
            f"Rejected builds touched {rejected_mean} modules on average against "
            f"{accepted_mean} for accepted ones. Prefer decomposition above "
            f"{max(limit, 1)} modules."),
        evidence={"reviewed": reviewed, "rejection_rate": rejection_rate, "sizes": sizes},
    )]


def _review_depth_candidates(store, facts):
    proposals = []
    for group, counts in sorted(facts["review_value_by_group"].items()):
        if counts["reviews"] < REVIEW_VALUE_SAMPLES or counts["rejections"] > 0:
            continue
        job_type, _, risk = group.partition(":")
        proposals.append(learnable.propose(
            store, f"review_depth.{group}", "review_depth",
            {"job_type": job_type, "risk": risk, "independent_review": False},
            rationale=(
                f"Independent review of {group} has found nothing in "
                f"{counts['reviews']} consecutive reviews. Consider lighter review "
                f"for this group and spend the reviews where they reject."),
            evidence=dict(counts, group=group),
        ))
    return proposals


def _retry_candidate(store, facts):
    failures = facts["failure_categories"]
    total = sum(failures.values())
    if total < MIN_SAMPLES:
        return []
    dominant, count = max(failures.items(), key=lambda pair: pair[1])
    if count / total < 0.6 or dominant == taxonomy.OTHER:
        return []
    return [learnable.propose(
        store, f"retry_strategy.{dominant}", "retry_strategy",
        {"category": dominant, "action": "raise_timeout" if dominant == "timeout"
         else "escalate_after_first_failure"},
        rationale=(f"{count} of {total} failures are '{dominant}'. Retrying the same way "
                   f"has not produced new evidence."),
        evidence=failures,
    )]


# --- rendering ------------------------------------------------------------


def render(facts, proposals):
    lines = [
        "Observations from recorded runs. Nothing here has been applied: every",
        "proposal below is a policy candidate awaiting an explicit adoption.",
        "",
        f"- jobs: {facts['jobs']}  builds: {facts['builds']}  "
        f"reviewed: {facts['reviewed_builds']}  rejected: {facts['rejected_builds']}",
        f"- first-pass review rate: {facts['first_pass_review_rate']}",
        f"- blocker categories: {facts['blocker_categories'] or 'none recorded'}",
        f"- failure categories: {facts['failure_categories'] or 'none recorded'}",
        f"- run statuses: {facts['runs'] or 'none recorded'}",
        f"- spend: ${facts['cost_usd']:.2f}",
        "",
    ]
    if not proposals:
        lines.append("No policy change is warranted by this evidence.")
        return "\n".join(lines)
    lines.append("## Candidate policy changes")
    for policy in proposals:
        lines += [
            "",
            f"### {policy['name']} v{policy['version']} ({policy['kind']}) — {policy['status']}",
            f"Observation: {policy['rationale']}",
            f"Proposed: `{policy['body']}`",
        ]
    return "\n".join(lines)
