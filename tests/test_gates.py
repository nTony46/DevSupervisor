"""A gate is a row, and only a recorded decision releases the job."""

from devsupervisor import gates
from devsupervisor.state import machine
from tests.support import HarnessTestCase


class GateTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.job = self.store.create_job(self.project["id"], "feature", "build", "widget")
        self.store.transition(self.job["id"], machine.READY, actor="scheduler")

    def test_opening_a_gate_parks_the_job(self):
        gates.open_gate(self.store, "budget", "Spend $40 on a paid A/B run?",
                        project_id=self.project["id"], job_id=self.job["id"])
        self.assertEqual(self.store.get_job(self.job["id"])["status"], machine.WAITING_HUMAN)

    def test_a_parked_job_is_never_dispatched_by_readiness(self):
        gates.open_gate(self.store, "budget", "Spend?", project_id=self.project["id"],
                        job_id=self.job["id"])
        promoted = [j["id"] for j in self.store.promote_ready(self.project["id"])]
        self.assertNotIn(self.job["id"], promoted)

    def test_approval_resumes_the_job_at_the_recorded_status(self):
        gate = gates.open_gate(self.store, "budget", "Spend?", project_id=self.project["id"],
                               job_id=self.job["id"], resume_status=machine.READY)
        decided = gates.decide(self.store, gate["id"], approved=True, actor="tony", note="ok")
        self.assertEqual(decided["status"], "APPROVED")
        self.assertEqual(decided["decided_by"], "tony")
        self.assertEqual(self.store.get_job(self.job["id"])["status"], machine.READY)

    def test_rejection_blocks_rather_than_resumes(self):
        gate = gates.open_gate(self.store, "destructive", "Delete the index?",
                               project_id=self.project["id"], job_id=self.job["id"])
        gates.decide(self.store, gate["id"], approved=False, actor="tony")
        self.assertEqual(self.store.get_job(self.job["id"])["status"], machine.BLOCKED)

    def test_a_job_waits_until_every_gate_is_decided(self):
        first = gates.open_gate(self.store, "budget", "Spend?", project_id=self.project["id"],
                                job_id=self.job["id"])
        second = gates.open_gate(self.store, "destructive", "Irreversible?",
                                 project_id=self.project["id"], job_id=self.job["id"])
        gates.decide(self.store, first["id"], approved=True, actor="tony")
        self.assertEqual(self.store.get_job(self.job["id"])["status"], machine.WAITING_HUMAN)
        gates.decide(self.store, second["id"], approved=True, actor="tony")
        self.assertEqual(self.store.get_job(self.job["id"])["status"], machine.READY)

    def test_deciding_twice_is_a_noop(self):
        gate = gates.open_gate(self.store, "budget", "Spend?", project_id=self.project["id"],
                               job_id=self.job["id"])
        gates.decide(self.store, gate["id"], approved=True, actor="tony")
        again = gates.decide(self.store, gate["id"], approved=False, actor="someone-else")
        self.assertEqual(again["status"], "APPROVED")
        self.assertEqual(again["decided_by"], "tony")

    def test_open_gates_are_listable_for_the_cli(self):
        gates.open_gate(self.store, "budget", "Spend?", project_id=self.project["id"],
                        job_id=self.job["id"])
        self.assertEqual(len(gates.open_gates(self.store, project_id=self.project["id"])), 1)


class CriticalDispatchGateTests(HarnessTestCase):
    """A CRITICAL job must not spend anything without a recorded approval.

    The planner opens gates for CRITICAL plans, but jobs can also be created
    directly — an entry path that had no gate at all. The guarantee therefore
    lives at dispatch, which every job passes through however it was created.
    """

    def setUp(self):
        super().setUp()
        from devsupervisor.providers.mock import MockProvider, completed
        from devsupervisor.supervisor import Supervisor
        self.project = self.make_project()
        self.supervisor = Supervisor(self.store,
                                     provider=MockProvider(default=completed()))

    def _critical_job(self):
        job = self.store.create_job(self.project["id"], "feature", "build",
                                    "destructive thing", risk="CRITICAL",
                                    worktree="/tmp/wt")
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        return job

    def test_an_ungated_critical_job_is_parked_not_dispatched(self):
        job = self._critical_job()
        self.supervisor.advance(self.store.get_job(job["id"]))

        self.assertEqual(self.supervisor.provider.calls, [])
        self.assertEqual(self.store.get_job(job["id"])["status"], machine.WAITING_HUMAN)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM runs").fetchone()["n"], 0)

    def test_the_requirement_is_recorded_as_a_gate_and_an_event(self):
        job = self._critical_job()
        self.supervisor.advance(self.store.get_job(job["id"]))
        open_now = gates.open_gates(self.store, job_id=job["id"])
        self.assertEqual([g["kind"] for g in open_now], ["destructive"])
        self.assertTrue(self.store.events(kind="critical.gate_required"))

    def test_an_approved_gate_lets_it_run(self):
        job = self._critical_job()
        gate = gates.open_gate(self.store, "destructive", "Approve?",
                               project_id=self.project["id"], job_id=job["id"])
        gates.decide(self.store, gate["id"], approved=True, actor="tony")
        self.supervisor.advance(self.store.get_job(job["id"]))
        self.assertEqual(len(self.supervisor.provider.calls), 1)

    def test_non_critical_work_is_unaffected(self):
        job = self.store.create_job(self.project["id"], "feature", "build", "ordinary",
                                    risk="HIGH", worktree="/tmp/wt")
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        self.supervisor.advance(self.store.get_job(job["id"]))
        self.assertEqual(len(self.supervisor.provider.calls), 1)


class CriticalGateScopeTests(HarnessTestCase):
    """The gate guards consequences, not inspection."""

    def setUp(self):
        super().setUp()
        from devsupervisor.providers.mock import MockProvider, approve, completed
        from devsupervisor.supervisor import Supervisor
        self.project = self.make_project()
        self.supervisor = Supervisor(self.store, provider=MockProvider(
            script={"reviewer": approve(), "evaluator": approve()},
            default=completed()))

    def _dispatch(self, role):
        job = self.store.create_job(self.project["id"], "feature", role, f"{role} work",
                                    risk="CRITICAL", worktree="/tmp/wt",
                                    review_policy="none")
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        self.supervisor.advance(self.store.get_job(job["id"]))
        return self.store.get_job(job["id"])

    def test_critical_review_runs_without_a_gate(self):
        # Gating review would put friction on the mechanism that makes CRITICAL
        # work safe to do at all.
        for role in ("reviewer", "evaluator", "security", "qa"):
            calls_before = len(self.supervisor.provider.calls)
            self._dispatch(role)
            self.assertGreater(len(self.supervisor.provider.calls), calls_before,
                               f"{role} should not need a gate")

    def test_critical_work_that_can_write_still_needs_one(self):
        job = self._dispatch("build")
        self.assertEqual(job["status"], machine.WAITING_HUMAN)
        self.assertEqual(self.supervisor.provider.calls, [])

    def test_the_exemption_follows_the_read_only_tool_policy(self):
        from devsupervisor.policy import tools
        from devsupervisor.policy.immutable import check_critical_gate
        from devsupervisor.errors import HumanGateRequired
        critical = {"id": "X-1", "risk": "CRITICAL"}
        for role in ("reviewer", "evaluator"):
            self.assertTrue(check_critical_gate(critical, [],
                                                read_only=tools.is_read_only(role)))
        for role in ("build", "landing"):
            with self.assertRaises(HumanGateRequired, msg=role):
                check_critical_gate(critical, [], read_only=tools.is_read_only(role))
