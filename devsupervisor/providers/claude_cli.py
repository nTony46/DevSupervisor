"""Claude Code headless adapter.

Shells out to the `claude` CLI in print mode and parses its JSON envelope. It is
paid, so it refuses to run unless a budget is configured and paid runs are
explicitly enabled — the default resolution never reaches this class.
"""

import json
import shutil
import subprocess

from ..policy import permissions as permission_policy
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


def _denial_summary(denials):
    """Compress the runtime's denial records to 'tool: what was attempted'."""
    summary = []
    for entry in denials or []:
        if not isinstance(entry, dict):
            summary.append(str(entry)[:200])
            continue
        tool = entry.get("tool_name") or entry.get("tool") or "?"
        payload = entry.get("tool_input") or entry.get("input") or {}
        detail = payload.get("command") or payload.get("file_path") or ""
        summary.append(f"{tool}: {str(detail)[:160]}".strip())
    return summary


class ClaudeCLIProvider(Provider):
    name = "claude-cli"
    is_paid = True

    def __init__(self, model=None, budget_usd=0.0, allow_paid=False, binary=BINARY,
                 extra_args=(), permission_mode="dontAsk"):
        self.model = model
        self.budget_usd = budget_usd
        self.allow_paid = allow_paid
        self.binary = binary
        self.extra_args = tuple(extra_args)
        self.permission_mode = permission_mode
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
        if request.model or self.model:
            argv += ["--model", request.model or self.model]
        if request.effort:
            argv += ["--effort", request.effort]
        budget = request.max_budget_usd
        if budget:
            argv += ["--max-budget-usd", str(budget)]
        if request.tools:
            argv += ["--allowedTools", *request.tools]
        if request.disallowed_tools:
            argv += ["--disallowedTools", *request.disallowed_tools]
        mode = request.permission_mode or self.permission_mode
        if mode:
            argv += ["--permission-mode", mode]
        if permission_policy.is_bypass(mode):
            # Both forms are documented by the installed CLI and compose: the
            # mode names the intent in a value the CLI itself validates, and the
            # explicit flag is the one its help text documents for this.
            argv += ["--dangerously-skip-permissions"]
        # Nobody is sitting at this terminal to answer a permission prompt, and a
        # run that silently waits for one is worse than a run that is denied.
        argv += ["--permission-prompts", "none"]
        # Only ever when the caller asked for one. A fallback model silently
        # answers a question with a weaker model than the one that was chosen.
        if request.fallback_model:
            argv += ["--fallback-model", request.fallback_model]
        argv.extend(self.extra_args)
        return argv

    def result_instructions(self, job=None):
        return (
            "\n---\n"
            "## How to return your result\n\n"
            "End your turn with exactly one fenced block in this form, and put "
            "nothing after it:\n\n"
            f"{RESULT_FENCE}\n"
            "{\n"
            '  "status": "COMPLETED" | "FAILED" | "BLOCKED" | "NEEDS_HUMAN",\n'
            '  "summary": "one or two sentences",\n'
            '  "verdict": "APPROVE" | "REJECT",            // reviewers and evaluators only\n'
            '  "blockers": ["concrete, reproducible defect", "..."],  // required when REJECT\n'
            '  "evidence": ["command run -> what it showed", "..."],\n'
            '  "artifacts": [{"kind": "report|diff|test_log", "uri": "path or sha", '
            '"summary": "..."}],\n'
            '  "result_sha": "sha of what you produced, if anything",\n'
            '  "metrics": {"files_inspected": 0, "modules_touched": 0},\n'
            '  "memory_candidates": [{"title": "...", "body": "...", "area": "lessons"}],\n'
            '  "subtask_requests": [{"role": "security", "scope": "...", "reason": "..."}]\n'
            "}\n"
            "```\n\n"
            "Omit fields that do not apply. A REJECT with no blockers, or a "
            "verdict with no evidence, will be rejected by the harness as an "
            "invalid result. Do not claim a check passed that you did not run.\n\n"
            "If one shell command is denied, that is a denial of that command "
            "form, not of shell access. Try a simpler form — drop environment "
            "prefixes and pipes, run it from the right directory — before "
            "concluding you cannot run the project's checks.\n"
        )

    @staticmethod
    def resolved_model(envelope, requested=None):
        """Which model actually did the work.

        `modelUsage` lists every model that billed, and the runtime routinely
        bills a small helper model alongside the one that did the reasoning.
        Taking the first key alphabetically reported a haiku helper as the model
        behind an Opus review — precisely the silent misreport this system
        exists to prevent. Prefer the model that was asked for; otherwise the one
        that cost the most, which is the one that did the thinking.
        """
        usage = envelope.get("modelUsage")
        if isinstance(usage, dict) and usage:
            if requested and requested in usage:
                return requested
            if requested:
                for key, entry in usage.items():
                    if (entry or {}).get("canonicalModel") == requested:
                        return key
            return max(usage.items(),
                       key=lambda pair: (pair[1] or {}).get("costUSD") or 0)[0]
        for key in ("model", "model_id"):
            if envelope.get(key):
                return str(envelope[key])
        return None

    @staticmethod
    def model_costs(envelope):
        usage = envelope.get("modelUsage") or {}
        return {model: (entry or {}).get("costUSD") or 0.0
                for model, entry in usage.items()}

    @staticmethod
    def token_counts(envelope, model):
        """Real token counts for one model.

        `usage.input_tokens` reports only the uncached input — 2 tokens on a run
        that actually read ten thousand from cache. Billing-accurate counts live
        in modelUsage.
        """
        entry = (envelope.get("modelUsage") or {}).get(model) or {}
        if entry:
            tokens_in = ((entry.get("inputTokens") or 0)
                         + (entry.get("cacheReadInputTokens") or 0)
                         + (entry.get("cacheCreationInputTokens") or 0))
            return tokens_in, entry.get("outputTokens") or 0, entry.get("thinkingTokens")
        usage = envelope.get("usage") or {}
        return usage.get("input_tokens"), usage.get("output_tokens"), None

    def run(self, request):
        self.preflight()
        self._requested_model = request.model or self.model
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
        return self._parse(completed.stdout, requested=self._requested_model)

    def _parse(self, stdout, requested=None):
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return RunOutcome(status=RUN_FAILED, error=f"unparseable provider output: {exc}")

        cost = envelope.get("total_cost_usd") or envelope.get("cost_usd") or 0.0
        self.spent_usd += float(cost)
        denials = _denial_summary(envelope.get("permission_denials"))
        requested = requested or getattr(self, "_requested_model", None)
        primary = self.resolved_model(envelope, requested)
        tokens_in, tokens_out, thinking = self.token_counts(envelope, primary)
        text = envelope.get("result") or envelope.get("text") or ""

        try:
            result = self.extract_result(text)
        except InvalidResult as exc:
            return RunOutcome(status=RUN_FAILED, error=str(exc),
                              session_id=envelope.get("session_id"), cost_usd=cost,
                              model_resolved=primary,
                              models_used=self.model_costs(envelope),
                              permission_denials=denials, raw_text=text)
        return RunOutcome(
            status=RUN_SUCCEEDED, result=result, session_id=envelope.get("session_id"),
            tokens_in=tokens_in, tokens_out=tokens_out, thinking_tokens=thinking,
            cost_usd=cost, exit_code=0, model_resolved=primary,
            models_used=self.model_costs(envelope),
            permission_denials=denials, turns=envelope.get("num_turns"),
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
