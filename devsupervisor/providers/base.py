"""The agent runtime abstraction.

Core orchestration is not Claude-specific. A provider runs one job and returns a
structured outcome; provider session ids are metadata, never workflow state.
"""

from dataclasses import dataclass, field

from ..errors import DevSupervisorError

RUN_SUCCEEDED = "SUCCEEDED"
RUN_FAILED = "FAILED"
RUN_TIMEOUT = "TIMEOUT"
RUN_CANCELLED = "CANCELLED"


class ProviderError(DevSupervisorError):
    """The runtime could not execute the job."""


class BudgetExceeded(ProviderError):
    """A paid run was attempted without, or beyond, a configured budget."""


@dataclass
class RunRequest:
    job_id: str
    role: str
    prompt: str
    workdir: str = None
    session_id: str = None
    model: str = None
    timeout_s: int = 900
    attempt: int = 1
    metadata: dict = field(default_factory=dict)


@dataclass
class RunOutcome:
    status: str
    result: object = None            # a WorkerResult, when the worker produced one
    session_id: str = None
    tokens_in: int = None
    tokens_out: int = None
    cost_usd: float = None
    transcript_path: str = None
    exit_code: int = None
    error: str = None

    @property
    def succeeded(self):
        return self.status == RUN_SUCCEEDED


class Provider:
    """Interface every runtime implements."""

    name = "abstract"
    is_paid = False

    def run(self, request):
        raise NotImplementedError

    def resume(self, request):
        """Continue an existing session. Defaults to a fresh run."""
        return self.run(request)

    def cancel(self, session_id):
        return False

    def status(self, session_id):
        return "UNKNOWN"

    def describe(self):
        return {"name": self.name, "paid": self.is_paid}
