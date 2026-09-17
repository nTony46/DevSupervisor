"""Deterministic labels for the dashboard.

Every string the dashboard shows about a job is computed here from durable
fields. No model is invoked: a status line that needed an LLM would be a status
line that could be wrong, and an observability surface that lies is worse than
no observability surface.
"""

import json

from ..state import machine

MAX_ACTION_CHARS = 52
MAX_SUBJECT_WORDS = 6

# What each role is doing while it works. An unknown role gets a neutral verb
# rather than a guess, so a new role added to DevSupervisor degrades quietly.
ROLE_VERBS = {
    "supervisor": "Supervising",
    "planner": "Planning",
    "architect": "Designing",
    "build": "Implementing",
    "reviewer": "Reviewing",
    "specialist": "Analysing",
    "security": "Auditing",
    "qa": "Verifying",
    "benchmark": "Benchmarking",
    "evaluator": "Evaluating",
    "researcher": "Researching",
    "investigator": "Investigating",
    "landing": "Landing",
    "freeze": "Freezing",
    "operator": "Operating",
}
DEFAULT_VERB = "Working on"

# Past tense, for the activity log.
ROLE_NOUNS = {
    "build": "build",
    "reviewer": "review",
    "landing": "landing",
    "evaluator": "evaluation",
}

# High-level pipeline stages, in order. Roles are grouped because the operator
# question is "where is this work", not "which of nine role names ran".
STAGE_ORDER = (
    ("Plan", ("planner", "architect")),
    ("Investigate", ("investigator", "researcher", "specialist", "benchmark")),
    ("Build", ("build",)),
    ("Review", ("reviewer", "security", "qa")),
    ("Land", ("landing", "freeze")),
    ("Evaluate", ("evaluator",)),
)

STAGE_COMPLETE, STAGE_ACTIVE, STAGE_UPCOMING, STAGE_BLOCKED = (
    "complete", "active", "upcoming", "blocked")

# Node status vocabulary for the agent graph. STALE is not a job status: it is
# a job claiming to run with nobody holding it, which the operator needs to see
# named rather than hidden or dressed up as work in progress.
AGENT_ACTIVE, AGENT_WAITING, AGENT_BLOCKED = "ACTIVE", "WAITING", "BLOCKED"
AGENT_COMPLETE, AGENT_FAILED, AGENT_IDLE = "COMPLETE", "FAILED", "IDLE"
AGENT_STALE = "STALE"

_ATTENTION = frozenset({machine.BLOCKED, machine.FAILED})
_UNSTARTED = frozenset({machine.PLANNED, machine.READY, machine.REVISION_READY})


def verb_for(role):
    return ROLE_VERBS.get((role or "").lower(), DEFAULT_VERB)


def subject_of(job):
    """A short human subject for a job.

    A job id is `ROLE-subject-slug-NNN`, which is already a hand-written summary
    of the work. It is preferred over `scope`, which is a full paragraph meant
    for the detail panel, and over a title only some jobs carry.
    """
    title = _metadata(job).get("title")
    if title:
        return _cap_words(str(title))
    return _cap_words(_subject_from_id(job.get("id") or "", job.get("role") or ""))


def _metadata(job):
    """Job metadata, whether it arrives decoded or as the raw JSON column."""
    raw = job.get("metadata") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _subject_from_id(job_id, role):
    remainder = job_id
    prefix = f"{role.upper()}-"
    if role and remainder.upper().startswith(prefix):
        remainder = remainder[len(prefix):]
    parts = [part for part in remainder.split("-") if part]
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(parts) if parts else (job_id or "work")


def _cap_words(text):
    words = str(text).split()
    if len(words) > MAX_SUBJECT_WORDS:
        words = words[:MAX_SUBJECT_WORDS]
    return " ".join(words)


def action_line(job):
    """One short sentence: what this agent is doing, right now."""
    subject = subject_of(job)
    status = job.get("status")
    if status == machine.WAITING_HUMAN:
        line = f"Waiting on a decision about {subject}"
    elif status in _ATTENTION:
        line = f"{status.title()} on {subject}"
    elif status == machine.LANDING:
        line = f"Landing {subject}"
    elif status in _UNSTARTED:
        line = f"Queued: {subject}"
    elif status in machine.TERMINAL:
        line = f"Finished {subject}"
    else:
        line = f"{verb_for(job.get('role'))} {subject}"
    return truncate(line, MAX_ACTION_CHARS)


def truncate(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def stalled_line(job):
    """A stalled job is not doing anything, so it must not read as if it were."""
    return truncate(f"Stalled, no lease holder: {subject_of(job)}", MAX_ACTION_CHARS)


def agent_status(job, recently_complete=False):
    """Map a durable job status onto the graph's visual vocabulary."""
    status = job.get("status")
    if status in machine.ACTIVE:
        return AGENT_ACTIVE
    if status == machine.WAITING_HUMAN:
        return AGENT_WAITING
    if status == machine.FAILED:
        return AGENT_FAILED
    if status in _ATTENTION:
        return AGENT_BLOCKED
    if status in machine.TERMINAL and recently_complete:
        return AGENT_COMPLETE
    return AGENT_IDLE


def stage_for_role(role):
    role = (role or "").lower()
    for stage, roles in STAGE_ORDER:
        if role in roles:
            return stage
    return None


def pipeline(jobs):
    """A high-level progression derived from the jobs actually in scope.

    Stages with no job are not shown: a pipeline is a description of this work,
    not a fixed six-box template that every project is pressed into.
    """
    grouped = {}
    for job in jobs:
        stage = stage_for_role(job.get("role"))
        if stage:
            grouped.setdefault(stage, []).append(job)
    stages = []
    for stage, _roles in STAGE_ORDER:
        members = grouped.get(stage)
        if members:
            stages.append({"name": stage, "state": _stage_state(members),
                           "jobs": len(members)})
    return stages


def _stage_state(jobs):
    statuses = [job.get("status") for job in jobs]
    if any(status in _ATTENTION or status == machine.WAITING_HUMAN for status in statuses):
        return STAGE_BLOCKED
    if any(status in machine.ACTIVE for status in statuses):
        return STAGE_ACTIVE
    if all(status in _UNSTARTED for status in statuses):
        return STAGE_UPCOMING
    if all(status in machine.TERMINAL for status in statuses):
        return STAGE_COMPLETE
    return STAGE_ACTIVE


def current_line(jobs):
    """The single line under the pipeline: what is happening right now."""
    for status_group in (machine.ACTIVE, {machine.WAITING_HUMAN}, _ATTENTION):
        for job in jobs:
            if job.get("status") in status_group:
                return action_line(job)
    return ""
