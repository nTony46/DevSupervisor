"""Crash, restart, resume — without redoing work that is already recorded."""

from datetime import datetime, timezone

from devsupervisor import clock
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.state import Store, leases, machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase


def script():
    return {
        "reviewer": approve("correct"),
        "evaluator": approve("goal met"),
        "build": completed("built", result_sha="sha-001"),
        "landing": completed("landed", result_sha="main-sha"),
    }


class CrashResumeTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.goal = self.store.create_goal(self.project["id"], "Add workspace support")

    def _run_until(self, supervisor, predicate, limit=20):
        for _ in range(limit):
            if predicate():
                return True
            jobs = supervisor.scheduler.ready_jobs(self.project["id"], limit=1)
            if not jobs:
                return predicate()
            supervisor.advance(jobs[0])
        return predicate()

    def test_resume_picks_up_at_landing_without_repeating_completed_work(self):
        first = Supervisor(self.store, provider=MockProvider(script=script(),
                                                             default=completed()))
        first.plan_goal(self.goal)
        build_id = "BUILD-add-workspace-support-001"

        reached = self._run_until(
            first,
            lambda: self.store.get_job(build_id)["status"] == machine.LANDING_READY)
        self.assertTrue(reached, "setup did not reach the approved-and-ready-to-land state")
        done_before = {j["id"] for j in self.store.list_jobs(self.project["id"],
                                                             status=machine.DONE)}
        self.assertIn("REVIEWER-add-workspace-support-001", done_before)

        # Crash: drop every handle, then come back with a fresh process-like store.
        self.store.close()
        self.store = Store.open()
        resumed_provider = MockProvider(script=script(), default=completed())
        second = Supervisor(self.store, provider=resumed_provider)
        summary = second.run(self.project["id"])

        dispatched = {call.job_id for call in resumed_provider.calls}
        self.assertEqual(dispatched, {"LANDING-add-workspace-support-001",
                                      "EVALUATOR-add-workspace-support-001"})
        for finished in done_before:
            self.assertNotIn(finished, dispatched)
        self.assertEqual(self.store.get_job(build_id)["status"], machine.DONE)
        self.assertEqual(summary["stopped_because"], "all jobs complete")

    def test_a_worker_that_dies_mid_run_is_recovered_by_lease_expiry(self):
        clock.freeze(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
        supervisor = Supervisor(self.store, provider=MockProvider(script=script(), default=completed()))
        supervisor.plan_goal(self.goal)
        job = supervisor.scheduler.ready_jobs(self.project["id"], limit=1)[0]

        # A worker took the job and never came back.
        leases.acquire(self.store, job["id"], owner="dead-worker", ttl_seconds=60)
        self.store.transition(job["id"], machine.DISPATCHED, actor="scheduler")
        self.store.transition(job["id"], machine.RUNNING, actor="dead-worker")
        self.assertEqual(supervisor.scheduler.ready_jobs(self.project["id"]), [])

        clock.advance(61)
        recovered = supervisor.scheduler.ready_jobs(self.project["id"], limit=1)
        self.assertEqual([j["id"] for j in recovered], [job["id"]])
        self.assertEqual(self.store.events(kind="lease.reclaimed")[0]["payload"]
                         ["previous_owner"], "dead-worker")

    def test_the_supervisor_lock_is_released_after_a_run(self):
        supervisor = Supervisor(self.store, provider=MockProvider(script=script(), default=completed()))
        supervisor.plan_goal(self.goal)
        supervisor.run(self.project["id"])
        self.assertIsNone(leases.supervisor_lock_status(self.store))
