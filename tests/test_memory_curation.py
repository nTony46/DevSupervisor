"""Curation proposes. It never overwrites."""

from devsupervisor.memory import MemoryStore, candidates, curator
from tests.support import HarnessTestCase


class CurationTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.memory = MemoryStore(self.project["id"])

    def test_curation_finds_duplicates_and_writes_only_a_candidate(self):
        body = ("Landing jobs receive the exact reviewed SHA rather than a branch name, "
                "because a branch can move between review and landing.")
        self.memory.write("decisions", "Exact SHA landing", body, slug="exact-sha-landing")
        self.memory.write("conventions", "Exact SHA landing", body + " Always.",
                          slug="exact-sha-landing-2")

        report, path = curator.curate(self.project["id"])

        self.assertEqual(len(report["duplicates"]), 1)
        self.assertTrue(path.exists())
        self.assertIn("memory-candidates", str(path))
        # Authoritative memory is byte-identical afterwards.
        self.assertEqual(self.memory.read("decisions", "exact-sha-landing")["body"].strip(),
                         body)
        self.assertEqual(len(candidates.list_candidates(self.project["id"])), 1)

    def test_conflicting_documents_are_reported_separately_from_duplicates(self):
        body = ("Reviewers must not fix the code they review; they return blockers "
                "and the builder revises against exactly those blockers.")
        self.memory.write("conventions", "Reviewer discipline", body)
        self.memory.write("lessons", "What reviewers do", body + " No exceptions.")
        report = curator.analyse(self.project["id"])
        self.assertEqual(report["duplicates"], [])
        self.assertEqual(len(report["conflicts"]), 1)

    def test_clean_memory_produces_an_honest_empty_proposal(self):
        self.memory.write("product", "Goal", "Ship the private alpha.")
        report, path = curator.curate(self.project["id"])
        self.assertEqual(report["duplicates"], [])
        self.assertIn("No duplicates, conflicts, or stale documents found.", path.read_text())
