"""Self-improvement produces reviewable candidates and nothing else."""

from devsupervisor import metrics, retrospective
from devsupervisor.memory import candidates as memory_candidates
from devsupervisor.policy import immutable, learnable
from devsupervisor.state import machine
from tests.support import HarnessTestCase


class SyntheticHistory(HarnessTestCase):
    """Builds a run history directly, so the analysis is tested, not the loop."""

    def setUp(self):
        super().setUp()
        self.project = self.make_project()

    def build(self, subject, rejected, modules, job_type="feature", risk="MEDIUM"):
        job = self.store.create_job(
            self.project["id"], job_type, "build", subject, risk=risk,
            review_policy="independent")
        self.drive(job["id"], [machine.READY, machine.DISPATCHED, machine.RUNNING,
                               machine.WORK_COMPLETE, machine.UNDER_REVIEW],
                   actor=f"worker:{job['id']}")
        metrics.record(self.store, "worker.modules_touched", value=modules, job_id=job["id"])
        if rejected:
            self.store.transition(job["id"], machine.REJECTED, actor="worker:reviewer")
            metrics.record(self.store, "review.blocker",
                           text="also changed unrelated files, out of scope",
                           job_id=job["id"])
        else:
            self.store.transition(job["id"], machine.APPROVED, actor="worker:reviewer",
                                  fields={"result_sha": "sha"})
        return job


class DecompositionCandidateTests(SyntheticHistory):
    def test_repeated_rejection_of_large_jobs_proposes_decomposition(self):
        for index in range(4):
            self.build(f"large lane {index}", rejected=True, modules=6)
        for index in range(2):
            self.build(f"small lane {index}", rejected=False, modules=1)

        result = retrospective.retrospect(self.store, self.project["id"])
        names = [c["name"] for c in result["candidates"]]
        self.assertIn("decomposition.max_modules", names)
        candidate = next(c for c in result["candidates"]
                         if c["name"] == "decomposition.max_modules")
        self.assertEqual(candidate["status"], "CANDIDATE")
        self.assertIn("modules", candidate["rationale"])

    def test_a_healthy_history_proposes_nothing(self):
        for index in range(6):
            self.build(f"lane {index}", rejected=False, modules=1)
        result = retrospective.retrospect(self.store, self.project["id"])
        self.assertEqual([c["name"] for c in result["candidates"]
                          if c["name"].startswith("decomposition")], [])

    def test_two_data_points_are_not_a_policy(self):
        self.build("lane a", rejected=True, modules=9)
        self.build("lane b", rejected=True, modules=9)
        result = retrospective.retrospect(self.store, self.project["id"])
        self.assertEqual(result["candidates"], [])


class ReviewDepthCandidateTests(SyntheticHistory):
    def test_review_that_never_rejects_proposes_lighter_review(self):
        for index in range(6):
            self.build(f"docs lane {index}", rejected=False, modules=1,
                       job_type="docs", risk="LOW")
        result = retrospective.retrospect(self.store, self.project["id"])
        names = [c["name"] for c in result["candidates"]]
        self.assertIn("review_depth.docs:LOW", names)
        candidate = next(c for c in result["candidates"]
                         if c["name"] == "review_depth.docs:LOW")
        self.assertEqual(candidate["body"]["independent_review"], False)
        self.assertEqual(candidate["status"], "CANDIDATE")

    def test_a_group_that_rejects_keeps_its_review(self):
        for index in range(6):
            self.build(f"code lane {index}", rejected=index == 0, modules=1)
        names = [c["name"] for c in
                 retrospective.retrospect(self.store, self.project["id"])["candidates"]]
        self.assertNotIn("review_depth.feature:MEDIUM", names)


class GuardrailTests(SyntheticHistory):
    def test_candidates_are_not_auto_adopted(self):
        for index in range(4):
            self.build(f"large lane {index}", rejected=True, modules=6)
        self.build("small lane", rejected=False, modules=1)
        result = retrospective.retrospect(self.store, self.project["id"])

        self.assertTrue(result["candidates"])
        for candidate in result["candidates"]:
            self.assertEqual(candidate["status"], "CANDIDATE")
            self.assertIsNone(learnable.active(self.store, candidate["name"]))
        adopted = learnable.list_policies(self.store, status="ADOPTED")
        self.assertEqual(adopted, [])

    def test_immutable_rules_are_unchanged_by_a_retrospective(self):
        before = immutable.rules()
        for index in range(5):
            self.build(f"lane {index}", rejected=True, modules=8)
        retrospective.retrospect(self.store, self.project["id"])
        self.assertEqual(immutable.rules(), before)
        self.assertEqual(
            [p for p in learnable.list_policies(self.store)
             if p["name"].startswith(("immutable", "safety", "hard_rule"))], [])

    def test_the_retrospective_writes_a_memory_candidate_not_memory(self):
        from devsupervisor.memory import MemoryStore
        for index in range(4):
            self.build(f"lane {index}", rejected=True, modules=6)
        result = retrospective.retrospect(self.store, self.project["id"])
        self.assertIsNotNone(result["memory_candidate"])
        self.assertEqual(MemoryStore(self.project["id"]).list("orchestration"), [])
        proposed = memory_candidates.list_candidates(self.project["id"])
        self.assertEqual([c["kind"] for c in proposed], ["retrospective"])
        self.assertIn("Nothing here has been applied", proposed[0]["body"])


class BlockerTaxonomyTests(SyntheticHistory):
    def test_blocker_categories_are_counted(self):
        for index in range(3):
            self.build(f"lane {index}", rejected=True, modules=2)
        facts = retrospective.observations(self.store, self.project["id"])
        self.assertEqual(facts["blocker_categories"], {"scope": 3})
        self.assertEqual(facts["first_pass_review_rate"], 0.0)
