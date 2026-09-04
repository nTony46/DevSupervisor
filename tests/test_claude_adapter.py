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
