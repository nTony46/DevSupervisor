"""A builder grading itself is not evidence, so the harness makes it impossible."""

from devsupervisor.errors import ReviewerIndependenceViolation
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.state import machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase


class IndependenceTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.goal = self.store.create_goal(self.project["id"], "Add workspace support")
        self.supervisor = Supervisor(self.store, provider=MockProvider(
            script={"reviewer": approve("fine"), "evaluator": approve("fine"),
                    "build": completed("built", result_sha="sha")},
            default=completed()))
        self.plan = self.supervisor.plan_goal(self.goal)
        self.build_id = "BUILD-add-workspace-support-001"

    def _work_the_build(self):
        for _ in range(6):
            jobs = self.supervisor.scheduler.ready_jobs(self.project["id"], limit=1)
            if not jobs:
                break
            if jobs[0]["id"] == self.build_id:
                self.supervisor.advance(jobs[0])
                return
            self.supervisor.advance(jobs[0])

    def test_builder_cannot_approve_own_job(self):
        self._work_the_build()
        self.assertEqual(self.store.get_job(self.build_id)["status"], machine.UNDER_REVIEW)
        builder_actor = f"worker:{self.build_id}"
        self.assertIn(builder_actor, self.store.work_actors(self.build_id))
        with self.assertRaises(ReviewerIndependenceViolation):
            self.store.transition(self.build_id, machine.APPROVED, actor=builder_actor)

    def test_medium_risk_build_cannot_skip_review(self):
        self._work_the_build()
        job = self.store.get_job(self.build_id)
        self.assertNotEqual(job["review_policy"], "none")
        self.assertEqual(job["status"], machine.UNDER_REVIEW)

    def test_reviewer_runs_in_a_fresh_session(self):
        self.supervisor.run(self.project["id"])
        reviewer_calls = [c for c in self.supervisor.provider.calls
                          if c.role == "reviewer"]
        self.assertTrue(reviewer_calls)
        self.assertIsNone(reviewer_calls[0].session_id)

    def test_builder_reuses_its_session_across_a_revision(self):
        build_calls = [c for c in self.supervisor.provider.calls if c.role == "build"]
        self.supervisor.run(self.project["id"])
        build_calls = [c for c in self.supervisor.provider.calls if c.role == "build"]
        self.assertTrue(build_calls)
        self.assertEqual(self.store.get_job(self.build_id)["session_policy"], "reuse")
