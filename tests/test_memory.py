"""Memory is authoritative, so writing to it is deliberately hard."""

from devsupervisor.memory import MemoryStore, MemoryWriteRefused, candidates, redact
from tests.support import HarnessTestCase


class RedactionTests(HarnessTestCase):
    def test_env_style_secrets_are_redacted(self):
        text = (
            "DATABASE_URL=postgres://admin:hunter2@db/app\n"
            "export ANTHROPIC_API_KEY=sk-ant-abcdef1234567890abcdef\n"
            "GITHUB_TOKEN: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "MAX_WORKERS=4\n"
        )
        cleaned = redact.redact(text)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("sk-ant-abcdef1234567890abcdef", cleaned)
        self.assertNotIn("ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", cleaned)
        self.assertIn("MAX_WORKERS=4", cleaned)

    def test_private_key_blocks_are_redacted(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----"
        self.assertNotIn("MIIEow", redact.redact(text))

    def test_ordinary_prose_is_untouched(self):
        text = "The retrieval fix lands on main as b25efad; see docs/RETRIEVAL.md."
        self.assertEqual(redact.redact(text), text)


class MemoryStoreTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.memory = MemoryStore(self.project["id"])

    def test_write_and_read_round_trip(self):
        self.memory.write("decisions", "Exact SHA landing",
                          "Landing jobs receive the reviewed SHA, not a branch name.",
                          tags=["landing", "git"])
        document = self.memory.read("decisions", "exact-sha-landing")
        self.assertEqual(document["title"], "Exact SHA landing")
        self.assertEqual(document["tags"], ["landing", "git"])

    def test_secret_content_is_refused_not_silently_stored(self):
        with self.assertRaises(MemoryWriteRefused):
            self.memory.write("conventions", "Local env",
                              "ANTHROPIC_API_KEY=sk-ant-abcdef1234567890abcdef")
        self.assertEqual(self.memory.list("conventions"), [])

    def test_existing_document_is_never_silently_overwritten(self):
        self.memory.write("lessons", "Verify first", "original body")
        with self.assertRaises(MemoryWriteRefused):
            self.memory.write("lessons", "Verify first", "replacement body")
        self.assertEqual(self.memory.read("lessons", "verify-first")["body"].strip(),
                         "original body")

    def test_unknown_area_is_rejected(self):
        with self.assertRaises(ValueError):
            self.memory.write("wherever", "T", "b")

    def test_search_ranks_title_matches_above_body_mentions(self):
        self.memory.write("architecture", "Retrieval routing", "how routing picks a lane")
        self.memory.write("lessons", "Release checklist", "mentions retrieval once")
        results = self.memory.search(["retrieval", "routing"], limit=2)
        self.assertEqual(results[0]["title"], "Retrieval routing")


class CandidateTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.memory = MemoryStore(self.project["id"])

    def test_worker_claims_land_as_candidates_not_memory(self):
        candidates.propose(self.project["id"], "Dense search is slow",
                           "Observed 400ms p95.", area="lessons", source_job="BUILD-x-001")
        self.assertEqual(len(candidates.list_candidates(self.project["id"])), 1)
        self.assertEqual(self.memory.list("lessons"), [])

    def test_adoption_is_explicit_and_recorded(self):
        candidates.propose(self.project["id"], "Dense search is slow", "Observed 400ms p95.",
                           area="lessons")
        name = candidates.list_candidates(self.project["id"])[0]["name"]
        written = candidates.adopt(self.project["id"], name, actor="tony")
        self.assertTrue(written.exists())
        self.assertEqual(len(self.memory.list("lessons")), 1)
        self.assertEqual(candidates.list_candidates(self.project["id"]), [])
        archived = candidates.get(self.project["id"], name)
        self.assertEqual(archived["status"], "ADOPTED")

    def test_candidate_secrets_are_stripped_before_they_are_written(self):
        candidates.propose(self.project["id"], "Env note",
                           "AWS_SECRET_ACCESS_KEY=abcdef1234567890", area="conventions")
        body = candidates.list_candidates(self.project["id"])[0]["body"]
        self.assertNotIn("abcdef1234567890", body)

    def test_adoption_over_an_existing_document_needs_an_explicit_overwrite(self):
        self.memory.write("lessons", "Same title", "authoritative body")
        candidates.propose(self.project["id"], "Same title", "candidate body", area="lessons")
        name = candidates.list_candidates(self.project["id"])[0]["name"]
        with self.assertRaises(MemoryWriteRefused):
            candidates.adopt(self.project["id"], name, actor="tony")
        self.assertEqual(self.memory.read("lessons", "same-title")["body"].strip(),
                         "authoritative body")
