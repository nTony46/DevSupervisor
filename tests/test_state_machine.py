"""The transition table is the harness's contract. Test it directly."""

from devsupervisor.errors import (
    IllegalTransition,
    ReviewerIndependenceViolation,
    TransitionGuardFailed,
)
from devsupervisor.state import machine
from tests.support import HarnessTestCase

HAPPY_PATH = [
    machine.READY, machine.DISPATCHED, machine.RUNNING, machine.WORK_COMPLETE,
    machine.UNDER_REVIEW,
]


class LegalTransitionTests(HarnessTestCase):
    def test_every_state_has_a_transition_entry(self):
        for state in machine.ALL_STATES:
            self.assertIn(state, machine.LEGAL, f"{state} missing from the table")

    def test_terminal_states_have_no_exits(self):
        for state in machine.TERMINAL:
            self.assertEqual(machine.LEGAL[state], frozenset(), f"{state} should be terminal")

    def test_full_happy_path_is_walkable(self):
        job = self.make_job()
        for status in HAPPY_PATH:
            job = self.store.transition(job["id"], status, actor="builder-a")
        job = self.store.transition(
            job["id"], machine.APPROVED, actor="reviewer-b", fields={"result_sha": "abc123"}
        )
        for status in (machine.LANDING_READY, machine.LANDING, machine.VERIFIED,
                       machine.EVALUATED, machine.DONE):
            job = self.store.transition(job["id"], status, actor="lander-c")
        self.assertEqual(job["status"], machine.DONE)

    def test_transition_history_is_recorded_in_order(self):
        job = self.make_job()
        self.store.transition(job["id"], machine.READY, actor="scheduler", reason="deps met")
        history = self.store.transitions(job["id"])
        self.assertEqual([h["to_status"] for h in history], [machine.PLANNED, machine.READY])
        self.assertEqual(history[-1]["reason"], "deps met")


class IllegalTransitionTests(HarnessTestCase):
    def test_skipping_review_is_rejected(self):
        job = self.make_job()
        self.store.transition(job["id"], machine.READY, actor="s")
        with self.assertRaises(IllegalTransition):
            self.store.transition(job["id"], machine.DONE, actor="s")

    def test_done_is_terminal(self):
        job = self.make_job(review_policy="none")
        self.drive(job["id"], [machine.READY, machine.DISPATCHED, machine.RUNNING,
                               machine.WORK_COMPLETE])
        self.store.transition(job["id"], machine.APPROVED, actor="w",
                              fields={"result_sha": "sha"})
        self.drive(job["id"], [machine.LANDING_READY, machine.LANDING, machine.VERIFIED,
                               machine.EVALUATED, machine.DONE])
        with self.assertRaises(IllegalTransition):
            self.store.transition(job["id"], machine.READY, actor="w")

    def test_unknown_state_is_rejected(self):
        job = self.make_job()
        with self.assertRaises(IllegalTransition):
            self.store.transition(job["id"], "PROBABLY_FINE", actor="s")

    def test_failed_transition_leaves_status_untouched(self):
        job = self.make_job()
        with self.assertRaises(IllegalTransition):
            self.store.transition(job["id"], machine.DONE, actor="s")
        self.assertEqual(self.store.get_job(job["id"])["status"], machine.PLANNED)


class GuardTests(HarnessTestCase):
    def _work_complete(self, **fields):
        job = self.make_job(**fields)
        self.drive(job["id"], [machine.READY, machine.DISPATCHED, machine.RUNNING,
                               machine.WORK_COMPLETE], actor="builder-a")
        return job

    def test_review_cannot_be_skipped_when_policy_requires_it(self):
        job = self._work_complete(review_policy="independent")
        with self.assertRaises(TransitionGuardFailed):
            self.store.transition(job["id"], machine.APPROVED, actor="reviewer-b")

    def test_low_risk_jobs_may_approve_without_review(self):
        job = self._work_complete(review_policy="none")
        approved = self.store.transition(job["id"], machine.APPROVED, actor="builder-a")
        self.assertEqual(approved["status"], machine.APPROVED)

    def test_builder_cannot_approve_its_own_work(self):
        job = self._work_complete(review_policy="independent")
        self.store.transition(job["id"], machine.UNDER_REVIEW, actor="supervisor")
        with self.assertRaises(ReviewerIndependenceViolation):
            self.store.transition(job["id"], machine.APPROVED, actor="builder-a")

    def test_landing_requires_a_candidate_sha(self):
        job = self._work_complete(review_policy="none")
        self.drive(job["id"], [machine.APPROVED, machine.LANDING_READY], actor="builder-a")
        with self.assertRaises(TransitionGuardFailed):
            self.store.transition(job["id"], machine.LANDING, actor="lander")

    def test_revision_cap_blocks_endless_loops(self):
        job = self._work_complete(review_policy="independent", max_revisions=1)
        self.store.transition(job["id"], machine.UNDER_REVIEW, actor="supervisor")
        self.store.transition(job["id"], machine.REJECTED, actor="reviewer-b")
        self.store.transition(job["id"], machine.REVISION_READY, actor="supervisor",
                              fields={"revision_count": 1})
        self.store.transition(job["id"], machine.READY, actor="supervisor")
        self.drive(job["id"], [machine.DISPATCHED, machine.RUNNING, machine.WORK_COMPLETE,
                               machine.UNDER_REVIEW], actor="builder-a")
        self.store.transition(job["id"], machine.REJECTED, actor="reviewer-b")
        with self.assertRaises(TransitionGuardFailed):
            self.store.transition(job["id"], machine.REVISION_READY, actor="supervisor")


class DuplicateDiscoveryTests(HarnessTestCase):
    """Reconciliation can find a duplicate at any point, including after a stall."""

    def test_a_blocked_job_can_be_marked_duplicate(self):
        original = self.make_job(subject="same work")
        copy = self.store.create_job(original["project_id"], "feature", "build", "same work")
        self.store.transition(copy["id"], machine.BLOCKED, actor="supervisor")

        self.store.relate(copy["id"], original["id"], "DUPLICATE")
        marked = self.store.transition(copy["id"], machine.DUPLICATE, actor="supervisor",
                                       reason=f"duplicate of {original['id']}")
        self.assertEqual(marked["status"], machine.DUPLICATE)
        self.assertIsNotNone(self.store.get_job(copy["id"]))
        self.assertEqual(self.store.get_job(original["id"])["status"], machine.PLANNED)

    def test_a_failed_or_paused_job_can_be_marked_duplicate_too(self):
        project = self.make_project()
        for state in (machine.FAILED, machine.PAUSED):
            job = self.make_job(project=project, subject=f"work {state.lower()}")
            self.store.transition(job["id"], state, actor="supervisor")
            self.store.transition(job["id"], machine.DUPLICATE, actor="supervisor")
            self.assertEqual(self.store.get_job(job["id"])["status"], machine.DUPLICATE)

    def test_duplicate_remains_terminal(self):
        job = self.make_job()
        self.store.transition(job["id"], machine.BLOCKED, actor="supervisor")
        self.store.transition(job["id"], machine.DUPLICATE, actor="supervisor")
        from devsupervisor.errors import IllegalTransition
        with self.assertRaises(IllegalTransition):
            self.store.transition(job["id"], machine.READY, actor="supervisor")
