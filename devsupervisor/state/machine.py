"""The job state machine.

This is deterministic code on purpose. A model may propose that a job is done;
only this module decides whether "done" is reachable from where the job is.
"""

from ..errors import IllegalTransition, ReviewerIndependenceViolation, TransitionGuardFailed

# --- states ---------------------------------------------------------------

PLANNED = "PLANNED"
READY = "READY"
DISPATCHED = "DISPATCHED"
RUNNING = "RUNNING"
WORK_COMPLETE = "WORK_COMPLETE"
UNDER_REVIEW = "UNDER_REVIEW"
REJECTED = "REJECTED"
REVISION_READY = "REVISION_READY"
APPROVED = "APPROVED"
LANDING_READY = "LANDING_READY"
LANDING = "LANDING"
VERIFIED = "VERIFIED"
EVALUATED = "EVALUATED"
DONE = "DONE"

BLOCKED = "BLOCKED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
SUPERSEDED = "SUPERSEDED"
DUPLICATE = "DUPLICATE"
WAITING_HUMAN = "WAITING_HUMAN"
PAUSED = "PAUSED"

TERMINAL = frozenset({DONE, CANCELLED, SUPERSEDED, DUPLICATE})

# The main path, in order. Used to decide whether a dependency edge is satisfied:
# "upstream has reached at least X". Off-path states rank below everything.
PROGRESS = (
    PLANNED, READY, DISPATCHED, RUNNING, WORK_COMPLETE, UNDER_REVIEW, APPROVED,
    LANDING_READY, LANDING, VERIFIED, EVALUATED, DONE,
)

# Only this role produces something that can be landed. Everything else is done
# when it is approved.
LANDABLE_ROLES = frozenset({"build"})


def reached(status, target):
    """True when `status` is at or past `target` on the main path."""
    if status not in PROGRESS or target not in PROGRESS:
        return False
    return PROGRESS.index(status) >= PROGRESS.index(target)
TERMINAL_SUCCESS = frozenset({DONE})
ACTIVE = frozenset({DISPATCHED, RUNNING, LANDING})

# States from which the generic "something went wrong / step aside" moves are
# always available. Keeping them in one place stops the table below from
# drowning in repetition.
_ESCAPES = frozenset({BLOCKED, FAILED, CANCELLED, WAITING_HUMAN, PAUSED, SUPERSEDED, DUPLICATE})

LEGAL = {
    PLANNED: {READY} | _ESCAPES,
    READY: {DISPATCHED} | _ESCAPES,
    DISPATCHED: {RUNNING, READY} | _ESCAPES,
    RUNNING: {WORK_COMPLETE, READY} | _ESCAPES,
    WORK_COMPLETE: {UNDER_REVIEW, APPROVED} | _ESCAPES,
    UNDER_REVIEW: {APPROVED, REJECTED} | _ESCAPES,
    REJECTED: {REVISION_READY} | _ESCAPES,
    REVISION_READY: {READY} | _ESCAPES,
    APPROVED: {LANDING_READY, DONE} | _ESCAPES,
    LANDING_READY: {LANDING} | _ESCAPES,
    LANDING: {VERIFIED} | _ESCAPES,
    VERIFIED: {EVALUATED} | _ESCAPES,
    EVALUATED: {DONE, REJECTED} | _ESCAPES,
    DONE: frozenset(),
    # Recovery states can rejoin the graph where the supervisor left it.
    BLOCKED: {PLANNED, READY, REVISION_READY, LANDING_READY, CANCELLED, WAITING_HUMAN, SUPERSEDED},
    FAILED: {PLANNED, READY, REVISION_READY, CANCELLED, BLOCKED, WAITING_HUMAN, SUPERSEDED},
    PAUSED: {PLANNED, READY, LANDING_READY, CANCELLED, BLOCKED},
    WAITING_HUMAN: {
        PLANNED, READY, REVISION_READY, APPROVED, LANDING_READY, LANDING,
        CANCELLED, BLOCKED, FAILED,
    },
    CANCELLED: frozenset(),
    SUPERSEDED: frozenset(),
    DUPLICATE: frozenset(),
}

ALL_STATES = frozenset(LEGAL)


def is_legal(from_status, to_status):
    return to_status in LEGAL.get(from_status, frozenset())


def check_transition(job_id, from_status, to_status):
    """Raise unless the edge exists. Invalid transitions fail loudly."""
    if to_status not in ALL_STATES:
        raise IllegalTransition(job_id, from_status, to_status, "unknown target state")
    if from_status not in LEGAL:
        raise IllegalTransition(job_id, from_status, to_status, "unknown source state")
    if to_status not in LEGAL[from_status]:
        raise IllegalTransition(job_id, from_status, to_status)


# --- guards ---------------------------------------------------------------
#
# Guards are preconditions on an edge that exists. They encode the rules the
# harness must own because a model cannot be trusted to enforce them about
# itself: who may approve, what landing requires, when review is skippable.


def guard_approval(job, actor, work_actors):
    """A builder may not approve its own work, and review is not skippable."""
    review_policy = job["review_policy"]
    if job["status"] == WORK_COMPLETE and review_policy != "none":
        raise TransitionGuardFailed(
            f"job {job['id']}: review_policy={review_policy!r} requires UNDER_REVIEW "
            f"before APPROVED"
        )
    if review_policy == "none":
        return
    if actor in work_actors:
        raise ReviewerIndependenceViolation(
            f"job {job['id']}: {actor!r} performed the work and cannot approve it"
        )


def guard_done_from_approved(job):
    """A job with something to land must go through landing, not around it."""
    if job["role"] in LANDABLE_ROLES:
        raise TransitionGuardFailed(
            f"job {job['id']}: role {job['role']!r} produces a landable candidate; "
            f"it must pass through LANDING_READY rather than closing at APPROVED"
        )


def guard_landing(job):
    """Landing needs an identified candidate to land."""
    if not job["result_sha"]:
        raise TransitionGuardFailed(
            f"job {job['id']}: cannot enter LANDING without an approved candidate sha"
        )


def guard_revision(job):
    """Targeted revisions are bounded; past the cap the supervisor must replan."""
    if job["revision_count"] >= job["max_revisions"]:
        raise TransitionGuardFailed(
            f"job {job['id']}: revision cap reached "
            f"({job['revision_count']}/{job['max_revisions']}); escalate instead of looping"
        )


GUARDED = {
    APPROVED: "approval",
    LANDING: "landing",
    REVISION_READY: "revision",
    DONE: "done_from_approved",
}
