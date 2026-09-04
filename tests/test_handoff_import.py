"""Reconciliation keys on verified facts, never on an agent number."""

import subprocess

from devsupervisor import config
from devsupervisor.handoffs import importer, parser, reconcile
from devsupervisor.policy import packs
from devsupervisor.state import machine
from tests.support import HarnessTestCase

COMPLETE_HANDOFF = """# Agent / Role
- **Agent number:** Agent 4
- **Role:** implementer

# Current Status
**COMPLETE** — pushed and merged.

| | |
|---|---|
| Branch | `feature/alpha` |
| HEAD | `{alpha}` |
| Base | `{base}` |
| Merged | **Yes** |
"""

OPEN_HANDOFF = """# Agent / Role
- **Agent number:** Agent 6
- **Role:** implementer

# Current Status
**COMPLETE** — finished, pushed, **not merged**, awaiting review.

| | |
|---|---|
| Branch | `feature/beta` |
| HEAD | `{beta}` |
| Base | `{base}` |
| Merged | **no** |
"""

# Same work, different session, different self-assigned agent number.
DUPLICATE_HANDOFF = """# Agent / Role
- **Agent number:** Agent 9

# Current Status
**WAITING** — same lane, reported by a different session.

| | |
|---|---|
| Branch | `feature/beta` |
| HEAD | `{beta}` |
"""

NO_ARTIFACT_HANDOFF = """# Agent / Role
- **Agent number:** Agent 2

# Current Status
**COMPLETE** — delivered in chat. Nothing was committed.
"""


class HandoffFixtureTests(HarnessTestCase):
    """A real git repository, so every verification runs against real git."""

    def setUp(self):
        super().setUp()
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self.base = self._commit("one.txt", "one")
        self._git("checkout", "-q", "-b", "feature/alpha")
        self.alpha = self._commit("alpha.txt", "alpha")
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--ff-only", "feature/alpha")
        self._git("checkout", "-q", "-b", "feature/beta", self.base)
        self.beta = self._commit("beta.txt", "beta")
        self._git("checkout", "-q", "main")
        self._git("branch", "-f", "origin-main-stand-in", "main")

        self.directory = self.home / "handoffs"
        self.directory.mkdir()
        self._write("agent4-alpha.md", COMPLETE_HANDOFF)
        self._write("agent6-beta.md", OPEN_HANDOFF)
        self._write("agent9-beta-again.md", DUPLICATE_HANDOFF)
        self._write("agent2-chat-only.md", NO_ARTIFACT_HANDOFF)

        self.project = self.store.create_project("fixture", str(self.repo))
        config.ensure_project_dirs(self.project["id"])

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def _commit(self, name, text):
        (self.repo / name).write_text(text)
        self._git("add", "-A")
        self._git("-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", name)
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    def _write(self, name, template):
        (self.directory / name).write_text(
            template.format(alpha=self.alpha, beta=self.beta, base=self.base))

    def reconciled(self):
        return reconcile.reconcile(parser.load_all(self.directory), repo=str(self.repo),
                                   main_ref="origin-main-stand-in")


class ReconciliationTests(HandoffFixtureTests):
    def test_dedup_ignores_agent_number(self):
        jobs = {job.key: job for job in self.reconciled()}
        beta = jobs["branch:feature/beta"]
        self.assertEqual(beta.sources, ["agent6-beta.md", "agent9-beta-again.md"])
        self.assertEqual(len(beta.duplicates), 1)
        self.assertEqual(len([k for k in jobs if k.endswith("feature/beta")]), 1)

    def test_merged_work_is_classified_complete_from_git(self):
        jobs = {job.key: job for job in self.reconciled()}
        self.assertEqual(jobs["branch:feature/alpha"].classification, "COMPLETE")
        self.assertTrue(jobs["branch:feature/alpha"].verified["merged"])

    def test_finished_but_unmerged_work_waits_for_review(self):
        jobs = {job.key: job for job in self.reconciled()}
        self.assertEqual(jobs["branch:feature/beta"].classification, "WAITING_REVIEW")

    def test_a_claim_with_no_artifact_is_unverified_not_believed(self):
        jobs = {job.key: job for job in self.reconciled()}
        chat_only = jobs["task:agent2-chat-only"]
        self.assertEqual(chat_only.claimed_status, "COMPLETE")
        self.assertEqual(chat_only.classification, "UNVERIFIED")

    def test_a_false_merge_claim_is_recorded_as_a_disagreement(self):
        (self.directory / "agent7-lies.md").write_text(
            "# Current Status\n**COMPLETE**\n\n| Branch | `feature/beta` |\n"
            f"| HEAD | `{self.beta}` |\n| Merged | **Yes** |\n")
        beta = {job.key: job for job in self.reconciled()}["branch:feature/beta"]
        self.assertTrue(any("merged" in d for d in beta.disagreements))
        self.assertFalse(beta.verified["merged"])


class LandedByContentTests(HandoffFixtureTests):
    def test_work_already_in_main_under_another_sha_is_superseded(self):
        # Land beta's content on main as a different commit.
        self._git("checkout", "-q", "main")
        (self.repo / "beta.txt").write_text("beta")
        self._git("add", "-A")
        self._git("-c", "user.email=t@e.com", "-c", "user.name=T",
                  "commit", "-q", "-m", "same content, different sha")
        self._git("branch", "-f", "origin-main-stand-in", "main")

        beta = {job.key: job for job in self.reconciled()}["branch:feature/beta"]
        self.assertEqual(beta.classification, "SUPERSEDED")
        self.assertTrue(beta.verified["landed_by_content"])


class ImportTests(HandoffFixtureTests):
    def test_import_creates_a_review_chain_for_open_work_only(self):
        report = importer.import_jobs(self.store, self.project, self.reconciled(),
                                      pack=packs.load("generic"))
        self.assertEqual(len(report["review_chains"]), 1)
        self.assertEqual(len(report["completed"]), 1)
        chain = report["review_chains"][0]

        build = self.store.get_job(chain["build"])
        self.assertEqual(build["status"], machine.UNDER_REVIEW)
        self.assertEqual(build["result_sha"], self.beta)
        reviewer = self.store.get_job(chain["reviewer"])
        self.assertEqual(reviewer["reviews_job_id"], build["id"])
        self.assertEqual(self.store.unmet_dependencies(reviewer["id"]), [])
        landing = self.store.get_job(chain["landing"])
        self.assertEqual(self.store.unmet_dependencies(landing["id"]),
                         [build["id"], reviewer["id"]])

    def test_completed_work_is_never_proposed_again(self):
        importer.import_jobs(self.store, self.project, self.reconciled(),
                             pack=packs.load("generic"))
        done = [j for j in self.store.list_jobs(self.project["id"], status=machine.DONE)]
        self.assertEqual([j["branch"] for j in done], ["feature/alpha"])
        ready = self.store.promotable(self.project["id"])
        self.assertNotIn("feature/alpha", [j["branch"] for j in ready])

    def test_unverified_claims_become_triage_jobs_not_work(self):
        report = importer.import_jobs(self.store, self.project, self.reconciled(),
                                      pack=packs.load("generic"))
        self.assertEqual(len(report["triage"]), 1)
        job = self.store.get_job(report["triage"][0])
        self.assertEqual(job["status"], machine.BLOCKED)
        self.assertIn("Triage required", job["scope"])

    def test_imported_jobs_record_where_they_came_from(self):
        report = importer.import_jobs(self.store, self.project, self.reconciled(),
                                      pack=packs.load("generic"))
        build = self.store.get_job(report["review_chains"][0]["build"])
        self.assertEqual(build["metadata"]["imported_from"],
                         ["agent6-beta.md", "agent9-beta-again.md"])
        self.assertEqual(build["metadata"]["claimed_status"], "COMPLETE")


class ReimportTests(HandoffFixtureTests):
    """Importing is not a one-off. Re-running it must not redo finished work."""

    def test_a_second_import_creates_no_duplicate_jobs(self):
        first = importer.import_jobs(self.store, self.project, self.reconciled(),
                                     pack=packs.load("generic"))
        before = len(self.store.list_jobs(self.project["id"]))

        second = importer.import_jobs(self.store, self.project, self.reconciled(),
                                      pack=packs.load("generic"))

        self.assertEqual(len(self.store.list_jobs(self.project["id"])), before)
        self.assertEqual(second["review_chains"], [])
        self.assertTrue(second["already_present"])
        tracked = {entry["job_id"] for entry in second["already_present"]}
        self.assertIn(first["review_chains"][0]["build"], tracked)

    def test_a_frozen_artifact_is_not_reproposed_for_review(self):
        report = importer.import_jobs(self.store, self.project, self.reconciled(),
                                      pack=packs.load("generic"))
        build_id = report["review_chains"][0]["build"]
        build = self.store.get_job(build_id)
        # Simulate the freeze lane recording its authoritative SHA.
        self.store.update_job(build_id,
                              metadata=dict(build["metadata"], frozen_sha=self.beta))

        states = importer.known_states(self.store, self.project["id"])
        self.assertEqual(states[self.beta], "FROZEN")
        jobs = reconcile.reconcile(parser.load_all(self.directory), repo=str(self.repo),
                                   main_ref="origin-main-stand-in", known_states=states)
        beta = {job.key: job for job in jobs}["branch:feature/beta"]
        self.assertEqual(beta.classification, "FROZEN")

    def test_landed_work_reads_as_complete_on_reimport(self):
        report = importer.import_jobs(self.store, self.project, self.reconciled(),
                                      pack=packs.load("generic"))
        build_id = report["review_chains"][0]["build"]
        build = self.store.get_job(build_id)
        self.store.update_job(build_id,
                              metadata=dict(build["metadata"], landed_sha=self.beta))
        states = importer.known_states(self.store, self.project["id"])
        self.assertEqual(states[self.beta], "COMPLETE")
