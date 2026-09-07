"""Only the supervisor creates jobs. A worker asks; it does not spawn."""

from devsupervisor import delegation
from devsupervisor.delegation import DelegationRefused
from devsupervisor.providers.base import Provider
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.results import WorkerResult
from devsupervisor.state import machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase

REQUEST = {"role": "security", "scope": "Review the new token handling for leakage.",
           "reason": "this touches credential storage"}


class StructuralTests(HarnessTestCase):
    def test_a_worker_has_no_way_to_reach_the_store(self):
        # The provider contract passes a prompt and returns a result. There is no
        # store, no session, and no job-creation call on it at all.
        surface = {name for name in dir(Provider) if not name.startswith("_")}
        self.assertEqual(surface, {"run", "resume", "cancel", "status", "describe",
                                   "result_instructions", "name", "is_paid"})
        # The point is the absence, not the list: nothing here reaches state.
        self.assertFalse(any("job" in name or "store" in name or "create" in name
                             for name in surface))

    def test_a_result_can_only_request_never_create(self):
        result = WorkerResult(status="COMPLETED", subtask_requests=[REQUEST])
        self.assertEqual(result.subtask_requests, [REQUEST])
        self.assertNotIn("create_job", dir(result))


class AuthorizationTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.parent = self.store.create_job(
            self.project["id"], "feature", "build", "token store",
            repo="/repo", branch="feat/tokens", base_sha="abc123")

    def test_the_supervisor_creates_the_job_the_worker_asked_for(self):
        created = delegation.authorize(self.store, self.parent, [REQUEST])
        self.assertEqual(len(created), 1)
        child = created[0]
        self.assertEqual(child["role"], "security")
        self.assertEqual(child["metadata"]["requested_by"], self.parent["id"])
        self.assertEqual(child["metadata"]["authorized_by"], "supervisor")

    def test_the_authorization_is_an_auditable_event(self):
        delegation.authorize(self.store, self.parent, [REQUEST])
        event = self.store.events(kind="subtask.authorized")[0]
        self.assertEqual(event["payload"]["requested_by"], self.parent["id"])
        self.assertEqual(event["payload"]["authorized_by"], "supervisor")

    def test_a_worker_cannot_request_a_landing(self):
        with self.assertRaises(DelegationRefused):
            delegation.authorize(self.store, self.parent,
                                 [{"role": "landing", "scope": "ship it"}])

    def test_a_request_without_scope_is_refused(self):
        with self.assertRaises(DelegationRefused):
            delegation.authorize(self.store, self.parent, [{"role": "qa"}])

    def test_an_unbounded_fan_out_request_becomes_a_human_gate(self):
        from devsupervisor import gates
        requests = [dict(REQUEST, scope=f"lane {index}") for index in range(9)]
        created = delegation.authorize(self.store, self.parent, requests)
        self.assertEqual(created, [])
        kinds = [g["kind"] for g in gates.open_gates(self.store, job_id=self.parent["id"])]
        self.assertIn("strategy", kinds)

    def test_a_requested_reviewer_still_cannot_be_the_parent_worker(self):
        child = delegation.authorize(
            self.store, self.parent,
            [{"role": "reviewer", "scope": "review the token change"}])[0]
        self.assertEqual(child["reviews_job_id"], self.parent["id"])
        self.assertEqual(child["session_policy"], "fresh")

    def test_risk_is_classified_for_the_child_not_inherited_blindly(self):
        child = delegation.authorize(
            self.store, self.parent,
            [{"role": "qa", "scope": "check the credential rotation path"}])[0]
        self.assertEqual(child["risk"], "HIGH")


class FanOutTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.parent = self.store.create_job(self.project["id"], "experiment", "build",
                                            "reuse ceiling")

    def test_the_supervisor_may_fan_out_parallel_children(self):
        supervisor = Supervisor(self.store, provider=MockProvider(default=completed()))
        children = supervisor.fan_out(self.parent, [
            {"role": "reviewer", "scope": "independent review A"},
            {"role": "specialist", "scope": "performance review"},
            {"role": "researcher", "scope": "survey prior art"},
        ])
        self.assertEqual(len(children), 3)
        self.assertTrue(all(c["metadata"]["parent_job"] == self.parent["id"]
                            for c in children))
        self.assertTrue(self.store.events(kind="delegation.fanned_out"))

    def test_fan_out_is_capped(self):
        with self.assertRaises(DelegationRefused):
            delegation.fan_out(self.store, self.parent,
                               [{"role": "qa", "scope": f"lane {i}"} for i in range(9)])

    def test_supervisor_fan_out_may_create_roles_a_worker_may_not_request(self):
        children = delegation.fan_out(self.store, self.parent,
                                      [{"role": "landing", "scope": "land the winner"}])
        self.assertEqual(children[0]["role"], "landing")


class EndToEndDelegationTests(HarnessTestCase):
    def test_a_workers_request_is_realised_through_the_supervisor_loop(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        provider = MockProvider(script={
            "build": completed("built", result_sha="sha",
                               subtask_requests=[{"role": "security",
                                                  "scope": "check the new token path",
                                                  "reason": "touches credentials"}]),
            "reviewer": approve("correct"), "evaluator": approve("met"),
        }, default=completed())
        supervisor = Supervisor(self.store, provider=provider)
        supervisor.plan_goal(goal)
        supervisor.run(project["id"])

        security = self.store.list_jobs(project["id"], role="security")
        self.assertEqual(len(security), 1)
        self.assertEqual(security[0]["metadata"]["requested_by"],
                         "BUILD-add-workspace-support-001")
        # The provider never created it: it appears only after the supervisor
        # authorized the request.
        event = self.store.events(kind="subtask.authorized")[0]
        self.assertEqual(event["payload"]["authorized_by"], "supervisor")


class MalformedRequestTests(HarnessTestCase):
    """A bad subtask request must not destroy the work that came with it."""

    def test_an_invalid_request_is_refused_without_losing_the_result(self):
        from devsupervisor.providers.mock import MockProvider, approve, completed
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        # A reviewer that approves, and also asks for a role it may not request.
        provider = MockProvider(script={
            "build": completed("built", result_sha="sha"),
            "reviewer": approve("correct", subtask_requests=[
                {"role": "harness", "scope": "something only the supervisor may create"}]),
            "evaluator": approve("met")}, default=completed())
        supervisor = Supervisor(self.store, provider=provider)
        supervisor.plan_goal(goal)
        supervisor.run(project["id"])

        # The verdict still landed: the build was approved and carried on.
        build = self.store.get_job("BUILD-add-workspace-support-001")
        self.assertEqual(build["status"], machine.DONE)
        reviewer = self.store.get_job("REVIEWER-add-workspace-support-001")
        self.assertEqual(reviewer["status"], machine.DONE)
        # And the refusal is recorded rather than silent.
        events = self.store.events(kind="subtask.refused")
        self.assertTrue(events)
        self.assertIn("harness", events[0]["payload"]["reason"])
        # No job was created for the refused request.
        self.assertEqual(self.store.list_jobs(project["id"], role="harness"), [])
