"""What a worker is told — and, more importantly, what it is not told."""

import json
import os
import subprocess
import sys

from devsupervisor import artifacts, config
from devsupervisor.context import ContextCompiler
from devsupervisor.memory import MemoryStore
from devsupervisor.state import machine
from tests.support import ROOT, HarnessTestCase


class PacketScopeTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(name="alpha")
        self.other = self.make_project(name="beta", repo_path=str(self.home / "beta"))
        MemoryStore(self.project["id"]).write(
            "architecture", "Retrieval routing",
            "Exact routing consults the identifier index before dense search.",
            tags=["retrieval"])
        MemoryStore(self.other["id"]).write(
            "architecture", "Retrieval routing",
            "BETA-ONLY-SECRET-DETAIL about beta's retrieval routing.",
            tags=["retrieval"])
        self.goal = self.store.create_goal(self.project["id"], "Improve retrieval routing")
        self.job = self.store.create_job(
            self.project["id"], "feature", "build", "routing", goal_id=self.goal["id"],
            scope="tighten exact retrieval routing", acceptance_criteria=["tests green"])
        self.compiler = ContextCompiler(self.store)

    def test_another_projects_memory_never_enters_the_packet(self):
        packet = self.compiler.compile(self.job)
        rendered = packet.render()
        self.assertIn("Exact routing consults the identifier index", rendered)
        self.assertNotIn("BETA-ONLY-SECRET-DETAIL", rendered)

    def test_included_memory_is_cited_by_path(self):
        packet = self.compiler.compile(self.job)
        self.assertTrue(any("architecture/retrieval-routing.md" in s for s in packet.sources))

    def test_packet_excludes_transcripts(self):
        transcript = artifacts.write(
            self.store, self.project["id"], self.job["id"], "transcript.md",
            "BUILDER-INTERNAL-MONOLOGUE about three abandoned approaches",
            "transcript")
        downstream = self.store.create_job(
            self.project["id"], "feature", "build", "followup",
            goal_id=self.goal["id"], depends_on=[self.job["id"]])
        rendered = self.compiler.compile(downstream).render()
        self.assertNotIn("BUILDER-INTERNAL-MONOLOGUE", rendered)
        self.assertNotIn(transcript["uri"], rendered)

    def test_secrets_cannot_reach_a_worker_through_the_packet(self):
        job = self.store.create_job(
            self.project["id"], "feature", "build", "envwork", goal_id=self.goal["id"],
            scope="use ANTHROPIC_API_KEY=sk-ant-abcdef1234567890abcdef when calling out")
        rendered = self.compiler.compile(job).render()
        self.assertNotIn("sk-ant-abcdef1234567890abcdef", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_memory_section_respects_its_budget(self):
        memory = MemoryStore(self.project["id"])
        for index in range(12):
            memory.write("lessons", f"Routing lesson {index}", "routing " * 400)
        compiler = ContextCompiler(self.store, memory_limit=3, memory_chars=500)
        rendered = compiler.compile(self.job).render()
        self.assertIn("(truncated)", rendered)
        self.assertLess(len(rendered), 20000)


class ReviewPacketTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.goal = self.store.create_goal(self.project["id"], "Add line ranges")
        self.builder = self.store.create_job(
            self.project["id"], "feature", "build", "line range", goal_id=self.goal["id"],
            branch="slice/line-range", base_sha="6ca4ddd",
            scope="populate line_range", non_goals="no ranking changes",
            acceptance_criteria=["spans populated"])
        artifacts.write(self.store, self.project["id"], self.builder["id"], "transcript.md",
                        "BUILDER-INTERNAL-MONOLOGUE", "transcript")
        artifacts.write(self.store, self.project["id"], self.builder["id"], "diff.patch",
                        "diff --git a/x b/x", "diff", summary="the candidate diff")
        self.store.update_job(self.builder["id"], result_sha="0d3f4a3")
        self.reviewer = self.store.create_job(
            self.project["id"], "feature", "reviewer", "line range", goal_id=self.goal["id"],
            reviews_job_id=self.builder["id"], depends_on=[self.builder["id"]])
        self.compiler = ContextCompiler(self.store)

    def test_reviewer_gets_the_exact_candidate_reference(self):
        rendered = self.compiler.compile(self.reviewer).render()
        self.assertIn("Candidate SHA: 0d3f4a3", rendered)
        self.assertIn("slice/line-range", rendered)
        self.assertIn("the candidate diff", rendered)

    def test_reviewer_does_not_inherit_the_builder_transcript(self):
        rendered = self.compiler.compile(self.reviewer).render()
        self.assertNotIn("BUILDER-INTERNAL-MONOLOGUE", rendered)

    def test_reviewer_is_told_not_to_fix_what_it_reviews(self):
        self.assertIn("Do not fix the code you are reviewing",
                      self.compiler.compile(self.reviewer).render())

    def test_revision_packet_carries_the_blockers_verbatim(self):
        blockers = ["line_range is off by one at file end",
                    "no test covers a single-line span"]
        revision = self.store.create_job(
            self.project["id"], "feature", "build", "line range", goal_id=self.goal["id"],
            revision_of=self.builder["id"], blockers=blockers, scope="fix the blockers only")
        rendered = self.compiler.compile(revision).render()
        for blocker in blockers:
            self.assertIn(blocker, rendered)
        self.assertIn("Fix exactly these, nothing else", rendered)

    def test_a_non_revision_job_gets_no_blocker_section(self):
        self.assertNotIn("Reviewer blockers", self.compiler.compile(self.builder).render())


CONTINUATION = """
import json, sys
sys.path.insert(0, {root!r})
from devsupervisor.state import Store
from devsupervisor.context import ContextCompiler
store = Store.open()
job = store.get_job({job_id!r})
packet = ContextCompiler(store).compile(job)
print(json.dumps({{"text": packet.render(), "sources": packet.sources}}))
"""


class FreshContextHandoffTests(HarnessTestCase):
    def test_fresh_process_rebuilds_the_packet_from_artifacts_alone(self):
        project = self.make_project(name="handoff")
        goal = self.store.create_goal(project["id"], "Land the fix")
        builder = self.store.create_job(
            project["id"], "feature", "build", "fix", goal_id=goal["id"],
            branch="fix/thing", scope="fix the thing")
        artifacts.write(self.store, project["id"], builder["id"], "report.md",
                        "Root cause: the index was consulted after dense search.",
                        "report", summary="build report")
        self.store.update_job(builder["id"], result_sha="abc1234")
        follower = self.store.create_job(
            project["id"], "feature", "build", "followup", goal_id=goal["id"],
            depends_on=[builder["id"]], scope="continue from the report")
        packet_path = ContextCompiler(self.store).persist(
            follower, ContextCompiler(self.store).compile(follower))
        self.assertTrue(packet_path.exists())
        self.store.close()

        result = subprocess.run(
            [sys.executable, "-c", CONTINUATION.format(root=str(ROOT), job_id=follower["id"])],
            capture_output=True, text=True, env=dict(os.environ), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertIn("build report", observed["text"])
        self.assertIn("report.md", " ".join(observed["sources"]))

        from devsupervisor.state import Store
        self.store = Store.open()
