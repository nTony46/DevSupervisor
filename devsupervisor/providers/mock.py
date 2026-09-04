"""A deterministic provider.

Every automated test runs on this. It costs nothing, touches no network, and
makes the whole lifecycle — including a rejection and a targeted revision —
reproducible.
"""

import itertools

from ..results import WorkerResult
from .base import RUN_FAILED, RUN_SUCCEEDED, Provider, RunOutcome


class MockProvider(Provider):
    """Replays scripted results, keyed by job id, then by role, then a default.

    A script entry is a list consumed one per attempt, so a job can be made to
    fail its first review and pass its second.
    """

    name = "mock"
    is_paid = False

    def __init__(self, script=None, default=None, record=None):
        # A bare result is shorthand for "always this"; a list is consumed per attempt.
        self.script = {
            key: (list(value) if isinstance(value, (list, tuple)) else [value])
            for key, value in (script or {}).items()
        }
        self.default = default
        self.calls = []
        self.record = record if record is not None else []
        self._sessions = itertools.count(1)

    def run(self, request):
        self.calls.append(request)
        self.record.append({"job_id": request.job_id, "role": request.role,
                            "session_id": request.session_id, "prompt": request.prompt,
                            "model": request.model, "effort": request.effort})
        payload = self._next(request)
        if payload is None:
            return RunOutcome(status=RUN_FAILED, error=f"no scripted result for {request.job_id}")
        if isinstance(payload, Exception):
            raise payload
        result = payload if isinstance(payload, WorkerResult) else WorkerResult.from_dict(payload)
        session_id = request.session_id or f"mock-session-{next(self._sessions)}"
        return RunOutcome(
            status=RUN_SUCCEEDED, result=result, session_id=session_id,
            tokens_in=1000, tokens_out=500, cost_usd=0.0, exit_code=0,
            model_resolved=request.model,
        )

    def _next(self, request):
        for key in (request.job_id, request.role):
            queue = self.script.get(key)
            if queue:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(self.default):
            return self.default(request)
        return self.default

    def prompts_for(self, job_id):
        return [call.prompt for call in self.calls if call.job_id == job_id]


def completed(summary="done", **fields):
    """Shorthand for the common scripted success."""
    return WorkerResult(status="COMPLETED", summary=summary, **fields)


def approve(summary="looks correct", **fields):
    return WorkerResult(status="COMPLETED", summary=summary, verdict="APPROVE", **fields)


def reject(*blockers, summary="blocking defects found", **fields):
    return WorkerResult(status="COMPLETED", summary=summary, verdict="REJECT",
                        blockers=list(blockers), **fields)
