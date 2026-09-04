"""The Claude Code adapter, exercised without ever invoking it."""

from devsupervisor.providers import RunRequest
from devsupervisor.providers.claude_cli import RESULT_FENCE, ClaudeCLIProvider
from devsupervisor.results import InvalidResult
from tests.support import HarnessTestCase

RESULT_BLOCK = f"""
Here is what I did.

{RESULT_FENCE}
{{"status": "COMPLETED", "summary": "landed", "result_sha": "abc1234"}}
```
"""


class ClaudeAdapterTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.provider = ClaudeCLIProvider(model="claude-opus-5", budget_usd=10.0,
                                          allow_paid=True)

    def test_argv_uses_print_mode_and_json_output(self):
        argv = self.provider.build_argv(RunRequest(job_id="BUILD-x-001", role="build",
                                                   prompt="do the thing"))
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--model", argv)

    def test_a_reused_session_is_resumed_rather_than_restarted(self):
        argv = self.provider.build_argv(RunRequest(
            job_id="BUILD-x-001", role="build", prompt="p", session_id="sess-42"))
        self.assertIn("--resume", argv)
        self.assertIn("sess-42", argv)

    def test_structured_result_is_extracted_from_the_final_message(self):
        result = ClaudeCLIProvider.extract_result(RESULT_BLOCK)
        self.assertEqual(result.result_sha, "abc1234")
        self.assertEqual(result.status, "COMPLETED")

    def test_a_missing_result_block_is_a_failure_not_a_guess(self):
        with self.assertRaises(InvalidResult):
            ClaudeCLIProvider.extract_result("I think it works!")

    def test_unparseable_output_is_reported_as_a_failed_run(self):
        outcome = self.provider._parse("not json at all")
        self.assertFalse(outcome.succeeded)
        self.assertIn("unparseable", outcome.error)

    def test_cost_is_accumulated_from_the_provider_envelope(self):
        import json
        envelope = json.dumps({"result": RESULT_BLOCK, "total_cost_usd": 0.42,
                               "session_id": "sess-9",
                               "usage": {"input_tokens": 10, "output_tokens": 20}})
        outcome = self.provider._parse(envelope)
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.session_id, "sess-9")
        self.assertEqual(self.provider.spent_usd, 0.42)

    def test_describe_reports_whether_the_binary_is_present(self):
        described = self.provider.describe()
        self.assertIn("binary_on_path", described)
        self.assertTrue(described["paid"])


# Captured from a real `claude -p --output-format json` run. The runtime bills a
# small helper model alongside the model that does the reasoning, and reports
# only uncached input under `usage`.
REAL_ENVELOPE = {
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 1112, "outputTokens": 12, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": 0.001172,
            "canonicalModel": "claude-haiku-4-5", "thinkingTokens": 0,
        },
        "claude-opus-5": {
            "inputTokens": 2, "outputTokens": 4, "cacheReadInputTokens": 10123,
            "cacheCreationInputTokens": 8257, "costUSD": 0.0877415,
            "canonicalModel": "claude-opus-5", "thinkingTokens": 512,
        },
    },
    "usage": {"input_tokens": 2, "output_tokens": 4},
    "total_cost_usd": 0.0889135,
    "session_id": "sess-real",
}


class PrimaryModelAccountingTests(HarnessTestCase):
    """A run record that names the wrong model is worse than none."""

    def test_the_requested_model_is_reported_not_the_helper(self):
        self.assertEqual(
            ClaudeCLIProvider.resolved_model(REAL_ENVELOPE, "claude-opus-5"),
            "claude-opus-5")

    def test_without_a_request_the_costliest_model_is_the_primary_one(self):
        # Alphabetical order would pick the haiku helper here.
        self.assertEqual(ClaudeCLIProvider.resolved_model(REAL_ENVELOPE), "claude-opus-5")

    def test_a_canonical_name_matches_a_dated_model_id(self):
        self.assertEqual(
            ClaudeCLIProvider.resolved_model(REAL_ENVELOPE, "claude-haiku-4-5"),
            "claude-haiku-4-5-20251001")

    def test_token_counts_include_cache_reads(self):
        tokens_in, tokens_out, thinking = ClaudeCLIProvider.token_counts(
            REAL_ENVELOPE, "claude-opus-5")
        # `usage.input_tokens` says 2; the run actually read 18,382.
        self.assertEqual(tokens_in, 2 + 10123 + 8257)
        self.assertEqual(tokens_out, 4)
        self.assertEqual(thinking, 512)

    def test_every_billing_model_is_recorded_not_just_the_primary(self):
        costs = ClaudeCLIProvider.model_costs(REAL_ENVELOPE)
        self.assertEqual(sorted(costs), ["claude-haiku-4-5-20251001", "claude-opus-5"])
        self.assertAlmostEqual(sum(costs.values()), 0.0889135, places=6)

    def test_the_parsed_outcome_carries_all_of_it(self):
        import json
        provider = ClaudeCLIProvider(budget_usd=10.0, allow_paid=True)
        envelope = dict(REAL_ENVELOPE, result=RESULT_BLOCK)
        outcome = provider._parse(json.dumps(envelope), requested="claude-opus-5")
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.model_resolved, "claude-opus-5")
        self.assertEqual(outcome.tokens_in, 18382)
        self.assertEqual(outcome.thinking_tokens, 512)
        self.assertIn("claude-haiku-4-5-20251001", outcome.models_used)


class WorkerContractTests(HarnessTestCase):
    def test_the_provider_tells_the_worker_how_to_answer(self):
        instructions = ClaudeCLIProvider().result_instructions()
        self.assertIn(RESULT_FENCE, instructions)
        self.assertIn("blockers", instructions)
        self.assertIn("did not run", instructions)

    def test_read_only_roles_get_no_writing_tools(self):
        from devsupervisor.errors import PolicyViolation
        from devsupervisor.policy import tools
        for role in ("reviewer", "evaluator", "security", "investigator"):
            self.assertTrue(tools.assert_read_only(role, tools.profile_for(role)), role)
            self.assertNotIn("Edit", tools.profile_for(role), role)
        with self.assertRaises(PolicyViolation):
            tools.assert_read_only("reviewer", tools.IMPLEMENT)

    def test_a_builder_may_edit_and_a_lander_may_not_force_push(self):
        from devsupervisor.policy import tools
        self.assertIn("Edit", tools.profile_for("build"))
        joined = " ".join(tools.profile_for("landing"))
        self.assertNotIn("--force", joined)
        self.assertNotIn("push -f", joined)
        self.assertNotIn("reset --hard", joined)

    def test_permission_prompts_are_never_left_to_nobody(self):
        from devsupervisor.providers import RunRequest
        argv = ClaudeCLIProvider().build_argv(
            RunRequest(job_id="R-1", role="reviewer", prompt="p"))
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertIn("--permission-mode", argv)


class UnstructuredOutputTests(HarnessTestCase):
    """A run that cost money and produced an unreadable answer must stay diagnosable."""

    def test_the_raw_answer_is_kept_when_the_contract_is_not_met(self):
        import json
        provider = ClaudeCLIProvider(budget_usd=10.0, allow_paid=True)
        envelope = json.dumps({"result": "I looked at it and it seems fine to me.",
                               "total_cost_usd": 1.12, "session_id": "s"})
        outcome = provider._parse(envelope, requested="claude-opus-5")
        self.assertFalse(outcome.succeeded)
        self.assertIn("did not emit", outcome.error)
        self.assertEqual(outcome.raw_text, "I looked at it and it seems fine to me.")
        self.assertEqual(outcome.cost_usd, 1.12)

    def test_a_valid_result_does_not_hoard_the_transcript(self):
        import json
        provider = ClaudeCLIProvider(budget_usd=10.0, allow_paid=True)
        outcome = provider._parse(json.dumps({"result": RESULT_BLOCK, "total_cost_usd": 0.1}),
                                  requested="claude-opus-5")
        self.assertTrue(outcome.succeeded)
        self.assertIsNone(outcome.raw_text)
