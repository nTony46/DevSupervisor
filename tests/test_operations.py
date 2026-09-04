"""Operational hardening: locks, interrupts, and staying resumable."""

import io
from contextlib import redirect_stdout
from datetime import datetime, timezone

from devsupervisor import clock
from devsupervisor.cli import EXIT_INTERRUPTED, main
from devsupervisor.errors import GracefulExit, LeaseError
from devsupervisor.providers.base import Provider
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.state import Store, leases, machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase


class InterruptingProvider(Provider):
    """Stands in for Ctrl-C arriving while a worker is running."""

    name = "interrupting"
    is_paid = False

    def run(self, request):
        raise GracefulExit()


class SupervisorLockTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        clock.freeze(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
        self.project = self.make_project()
        self.goal = self.store.create_goal(self.project["id"], "Add workspace support")

    def test_a_second_supervisor_cannot_drive_the_same_root(self):
        first = Supervisor(self.store, provider=MockProvider(default=completed()),
                           owner="supervisor-a")
        first.plan_goal(self.goal)
        leases.acquire_supervisor_lock(self.store, "supervisor-a", ttl_seconds=600)
        second = Supervisor(self.store, provider=MockProvider(default=completed()),
                            owner="supervisor-b")
        with self.assertRaises(LeaseError):
            second.run(self.project["id"])

    def test_a_stale_lock_does_not_block_the_next_run(self):
        leases.acquire_supervisor_lock(self.store, "dead-supervisor", ttl_seconds=60)
        clock.advance(61)
        supervisor = Supervisor(self.store, provider=MockProvider(
            script={"reviewer": approve(), "evaluator": approve(),
                    "build": completed(result_sha="sha")}, default=completed()),
            owner="supervisor-b")
        supervisor.plan_goal(self.goal)
        summary = supervisor.run(self.project["id"])
        self.assertEqual(summary["stopped_because"], "all jobs complete")

    def test_the_lock_is_released_even_when_a_run_raises(self):
        supervisor = Supervisor(self.store, provider=InterruptingProvider(),
                                owner="supervisor-a")
        supervisor.plan_goal(self.goal)
        with self.assertRaises(GracefulExit):
            supervisor.run(self.project["id"])
        self.assertIsNone(leases.supervisor_lock_status(self.store))


class InterruptTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(name="demo")
        self.goal = self.store.create_goal(self.project["id"], "Add workspace support")

    def test_an_interrupt_is_not_recorded_as_a_failed_run(self):
        supervisor = Supervisor(self.store, provider=InterruptingProvider())
        supervisor.plan_goal(self.goal)
        job = supervisor.scheduler.ready_jobs(self.project["id"], limit=1)[0]
        with self.assertRaises(GracefulExit):
            supervisor.advance(job)
        runs = self.store.conn.execute(
            "SELECT status FROM runs WHERE job_id = ?", (job["id"],)).fetchall()
        self.assertEqual([r["status"] for r in runs], ["RUNNING"])
        self.assertIsNone(leases.holder(self.store, job["id"]))

    def test_state_after_an_interrupt_is_still_resumable(self):
        supervisor = Supervisor(self.store, provider=InterruptingProvider())
        supervisor.plan_goal(self.goal)
        job = supervisor.scheduler.ready_jobs(self.project["id"], limit=1)[0]
        try:
            supervisor.advance(job)
        except GracefulExit:
            pass
        self.store.close()
        self.store = Store.open()

        resumed = Supervisor(self.store, provider=MockProvider(
            script={"reviewer": approve(), "evaluator": approve(),
                    "build": completed(result_sha="sha")}, default=completed()))
        summary = resumed.run(self.project["id"])
        self.assertEqual(summary["stopped_because"], "all jobs complete")
        self.assertIn(job["id"], summary["dispatched"])

    def test_the_cli_reports_an_interrupt_and_exits_130(self):
        supervisor = Supervisor(self.store, provider=InterruptingProvider())
        supervisor.plan_goal(self.goal)
        self.store.close()

        import devsupervisor.cli as cli
        original = cli._supervisor
        cli._supervisor = lambda store, project, args: Supervisor(
            store, provider=InterruptingProvider())
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["run", "demo"])
        finally:
            cli._supervisor = original
            self.store = Store.open()
        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertIn("devsup resume", buffer.getvalue())


class DryRunPurityTests(HarnessTestCase):
    def test_a_dry_run_changes_no_state_and_is_repeatable(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        supervisor = Supervisor(self.store, provider=MockProvider(default=completed()),
                                dry_run=True)
        supervisor.plan_goal(goal)
        before = [(j["id"], j["status"], j["attempt"]) for j in
                  self.store.list_jobs(project["id"])]

        first = supervisor.dry_run_report(project["id"])
        second = supervisor.dry_run_report(project["id"])

        after = [(j["id"], j["status"], j["attempt"]) for j in
                 self.store.list_jobs(project["id"])]
        self.assertEqual(before, after)
        self.assertEqual([e["job_id"] for e in first["ready"]],
                         [e["job_id"] for e in second["ready"]])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM runs")
                         .fetchone()["n"], 0)
        self.assertEqual(supervisor.provider.calls, [])

    def test_dispatch_refuses_to_run_in_dry_run_mode(self):
        project = self.make_project()
        job = self.store.create_job(project["id"], "feature", "build", "widget")
        supervisor = Supervisor(self.store, dry_run=True)
        with self.assertRaises(RuntimeError):
            supervisor.scheduler.dispatch(job)
