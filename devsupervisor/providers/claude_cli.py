"""Claude Code headless adapter.

Shells out to the `claude` CLI in print mode and parses its JSON envelope. It is
paid, so it refuses to run unless a budget is configured and paid runs are
explicitly enabled — the default resolution never reaches this class.
"""

import json
import shutil
import subprocess

from ..results import InvalidResult, WorkerResult
from .base import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    RUN_TIMEOUT,
    BudgetExceeded,
    Provider,
    ProviderError,
    RunOutcome,
)

BINARY = "claude"

# The worker is told to end its turn with this fenced block. Parsing a fence is
# brittle enough that the harness treats a missing one as a failed run rather
# than guessing what the worker meant.
RESULT_FENCE = "```devsupervisor-result"


class ClaudeCLIProvider(Provider):
    name = "claude-cli"
    is_paid = True

    def __init__(self, model=None, budget_usd=0.0, allow_paid=False, binary=BINARY,
                 extra_args=()):
        self.model = model
        self.budget_usd = budget_usd
        self.allow_paid = allow_paid
        self.binary = binary
        self.extra_args = tuple(extra_args)
        self.spent_usd = 0.0

    # --- availability -----------------------------------------------------

    @classmethod
    def available(cls, binary=BINARY):
        return shutil.which(binary) is not None

    def preflight(self):
        """Everything that must be true before a paid call. Raises otherwise."""
        if not self.allow_paid:
            raise BudgetExceeded(
                "paid runs are disabled; pass allow_paid=True and a budget to enable"
            )
        if self.budget_usd <= 0:
            raise BudgetExceeded("no budget configured for a paid provider")
        if self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"budget exhausted: spent ${self.spent_usd:.2f} of ${self.budget_usd:.2f}"
            )
        if not self.available(self.binary):
            raise ProviderError(f"{self.binary!r} is not on PATH")
        return True

    # --- execution --------------------------------------------------------

    def build_argv(self, request):
        """Exposed so tests can assert the command without executing it."""
        argv = [self.binary, "-p", request.prompt, "--output-format", "json"]
        if request.session_id:
            argv += ["--resume", request.session_id]
        if self.model or request.model:
            argv += ["--model", request.model or self.model]
        argv.extend(self.extra_args)
        return argv

    def run(self, request):
        self.preflight()
        argv = self.build_argv(request)
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=request.timeout_s,
                cwd=request.workdir,
            )
        except subprocess.TimeoutExpired:
            return RunOutcome(status=RUN_TIMEOUT, error=f"timed out after {request.timeout_s}s")
        except OSError as exc:
            raise ProviderError(f"could not start {self.binary!r}: {exc}") from exc

        if completed.returncode != 0:
            return RunOutcome(status=RUN_FAILED, exit_code=completed.returncode,
                              error=(completed.stderr or "").strip()[:2000])
        return self._parse(completed.stdout)

    def _parse(self, stdout):
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return RunOutcome(status=RUN_FAILED, error=f"unparseable provider output: {exc}")

        cost = envelope.get("total_cost_usd") or envelope.get("cost_usd") or 0.0
        self.spent_usd += float(cost)
        usage = envelope.get("usage") or {}
        text = envelope.get("result") or envelope.get("text") or ""

        try:
            result = self.extract_result(text)
        except InvalidResult as exc:
            return RunOutcome(status=RUN_FAILED, error=str(exc),
                              session_id=envelope.get("session_id"), cost_usd=cost)
        return RunOutcome(
            status=RUN_SUCCEEDED, result=result, session_id=envelope.get("session_id"),
            tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"),
            cost_usd=cost, exit_code=0,
        )

    @staticmethod
    def extract_result(text):
        """Pull the structured result out of the worker's final message."""
        if RESULT_FENCE not in text:
            raise InvalidResult(
                f"worker did not emit a {RESULT_FENCE} block; refusing to guess its outcome"
            )
        body = text.split(RESULT_FENCE, 1)[1]
        body = body.split("```", 1)[0]
        return WorkerResult.from_json(body.strip()).validate()

    def describe(self):
        return {"name": self.name, "paid": True, "binary_on_path": self.available(self.binary),
                "budget_usd": self.budget_usd, "spent_usd": self.spent_usd,
                "enabled": self.allow_paid}
