"""The structured worker handoff format.

A worker's turn ends by writing one of these. It is the only thing the
supervisor reads back: no transcript scraping, no free-text parsing, so a fresh
agent can continue from artifacts alone.
"""

import json
from dataclasses import asdict, dataclass, field

from .errors import DevSupervisorError

STATUSES = ("COMPLETED", "FAILED", "BLOCKED", "NEEDS_HUMAN")
VERDICTS = ("APPROVE", "REJECT")


class InvalidResult(DevSupervisorError):
    """A worker returned something the harness will not act on."""


@dataclass
class WorkerResult:
    status: str
    summary: str = ""
    verdict: str = None          # reviewers only
    blockers: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    memory_candidates: list = field(default_factory=list)
    # A worker may ask for help. Only the supervisor creates the job.
    subtask_requests: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    result_sha: str = None
    session_id: str = None
    notes: str = ""

    def validate(self, role=None):
        if self.status not in STATUSES:
            raise InvalidResult(f"status {self.status!r} not in {STATUSES}")
        if self.verdict is not None and self.verdict not in VERDICTS:
            raise InvalidResult(f"verdict {self.verdict!r} not in {VERDICTS}")
        if role in ("reviewer", "qa", "security", "evaluator"):
            if self.status == "COMPLETED" and self.verdict is None:
                raise InvalidResult(f"a {role} must return APPROVE or REJECT")
            if self.verdict == "REJECT" and not self.blockers:
                raise InvalidResult("a REJECT must name at least one blocker")
        return self

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {}, "")}

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, dict):
            raise InvalidResult(f"expected an object, got {type(payload).__name__}")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(payload) - allowed
        if unknown:
            raise InvalidResult(f"unknown result fields: {sorted(unknown)}")
        if "status" not in payload:
            raise InvalidResult("result is missing 'status'")
        return cls(**payload)

    @classmethod
    def from_json(cls, text):
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise InvalidResult(f"result is not valid JSON: {exc}") from exc
