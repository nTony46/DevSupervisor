"""Artifacts are the handoff medium, so they must be addressable and hashed."""

from devsupervisor import artifacts
from tests.support import HarnessTestCase


class ArtifactTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.job = self.store.create_job(self.project["id"], "feature", "build", "widget")

    def test_written_artifact_is_hashed_and_retrievable(self):
        artifact = artifacts.write(self.store, self.project["id"], self.job["id"],
                                   "report.md", "all tests pass", "report",
                                   summary="build report")
        self.assertEqual(artifact["sha256"], artifacts.sha256_text("all tests pass"))
        self.assertEqual(artifacts.get(self.store, artifact["id"])["uri"], artifact["uri"])
        self.assertEqual([a["id"] for a in artifacts.for_job(self.store, self.job["id"])],
                         [artifact["id"]])

    def test_commit_artifacts_need_no_file(self):
        artifact = artifacts.register(self.store, self.project["id"], "commit", "0d3f4a3",
                                      job_id=self.job["id"], summary="line range slice")
        self.assertIn("0d3f4a3", artifacts.reference(artifact))

    def test_summaries_are_redacted(self):
        artifact = artifacts.register(
            self.store, self.project["id"], "report", "x", job_id=self.job["id"],
            summary="used API_KEY=sk-abcdef1234567890 to reach the service")
        self.assertNotIn("sk-abcdef1234567890", artifact["summary"])

    def test_filter_by_kind(self):
        artifacts.write(self.store, self.project["id"], self.job["id"], "a.md", "a", "report")
        artifacts.write(self.store, self.project["id"], self.job["id"], "b.log", "b", "test_log")
        self.assertEqual(len(artifacts.for_job(self.store, self.job["id"], kind="test_log")), 1)
