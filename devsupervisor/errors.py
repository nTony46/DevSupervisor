"""Errors the harness raises. Each one names a rule that was broken."""


class DevSupervisorError(Exception):
    """Base class for every error this harness raises deliberately."""


class IllegalTransition(DevSupervisorError):
    """A job was asked to move between states that are not connected."""

    def __init__(self, job_id, from_status, to_status, detail=""):
        self.job_id = job_id
        self.from_status = from_status
        self.to_status = to_status
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"job {job_id}: {from_status} -> {to_status} is not a legal transition{suffix}"
        )


class TransitionGuardFailed(DevSupervisorError):
    """The transition exists on the graph but its precondition is unmet."""


class ReviewerIndependenceViolation(TransitionGuardFailed):
    """An actor tried to approve work it also performed."""


class LeaseError(DevSupervisorError):
    """A job's ownership lease could not be acquired, held, or released."""


class PolicyViolation(DevSupervisorError):
    """An immutable safety rule would be broken by this action."""


class HumanGateRequired(DevSupervisorError):
    """The action needs a recorded human decision that does not exist yet."""


class NotFound(DevSupervisorError):
    """A referenced entity does not exist."""
