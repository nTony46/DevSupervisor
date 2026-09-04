"""The structured handoff format is the only thing the supervisor reads back."""

from devsupervisor.results import InvalidResult, WorkerResult
from tests.support import HarnessTestCase


class ResultTests(HarnessTestCase):
    def test_round_trip(self):
        result = WorkerResult(status="COMPLETED", summary="done", result_sha="abc123",
                              artifacts=[{"kind": "diff", "uri": "/tmp/x.patch"}])
        restored = WorkerResult.from_json(result.to_json())
        self.assertEqual(restored.result_sha, "abc123")
        self.assertEqual(restored.artifacts[0]["kind"], "diff")

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(InvalidResult):
            WorkerResult(status="PROBABLY_FINE").validate()

    def test_reviewer_must_return_a_verdict(self):
        with self.assertRaises(InvalidResult):
            WorkerResult(status="COMPLETED", summary="looks fine").validate(role="reviewer")

    def test_reject_without_blockers_is_rejected(self):
        with self.assertRaises(InvalidResult):
            WorkerResult(status="COMPLETED", verdict="REJECT").validate(role="reviewer")

    def test_reject_with_blockers_is_accepted(self):
        result = WorkerResult(status="COMPLETED", verdict="REJECT",
                              blockers=["off by one"]).validate(role="reviewer")
        self.assertEqual(result.blockers, ["off by one"])

    def test_unknown_fields_are_refused_rather_than_ignored(self):
        with self.assertRaises(InvalidResult):
            WorkerResult.from_dict({"status": "COMPLETED", "vibes": "good"})

    def test_malformed_json_names_the_problem(self):
        with self.assertRaises(InvalidResult):
            WorkerResult.from_json("{not json")
