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
