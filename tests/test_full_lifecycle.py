"""The whole loop, end to end, on the deterministic provider.

Build, reject with two blockers, targeted revision, approve, land, verify,
evaluate, done — with no paid calls and no network.
"""

from devsupervisor.providers.mock import MockProvider, approve, completed, reject
from devsupervisor.state import machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase

BLOCKERS = ["line_range is off by one at end of file",
            "no test covers a single-line span"]


class FullLifecycleTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(name="demo")
        self.goal = self.store.create_goal(
            self.project["id"], "Add workspace support",
            acceptance_criteria=["single-repo behaviour unchanged"])
        self.plan = None

    def _provider(self):
        return MockProvider(script={
            "REVIEWER-add-workspace-support-001": reject(*BLOCKERS),
            "REVIEWER-add-workspace-support-002": approve("blockers addressed"),
            "BUILD-add-workspace-support-001": completed("first attempt", result_sha="sha-001"),
            "BUILD-add-workspace-support-002": completed("revised", result_sha="sha-002"),
            "landing": completed("fast-forwarded to main", result_sha="main-sha"),
            "evaluator": approve("goal criteria met"),
        }, default=completed())

    def _supervise(self):
        supervisor = Supervisor(self.store, provider=self._provider())
        self.plan = supervisor.plan_goal(self.goal)
        return supervisor

    def test_rejection_revision_approval_landing_and_evaluation(self):
        supervisor = self._supervise()
        summary = supervisor.run(self.project["id"])

        first_build = self.store.get_job("BUILD-add-workspace-support-001")
        revision = self.store.get_job("BUILD-add-workspace-support-002")
        self.assertEqual(first_build["status"], machine.SUPERSEDED)
        self.assertEqual(revision["status"], machine.DONE)
        self.assertEqual(revision["revision_of"], first_build["id"])
        self.assertEqual(revision["blockers"], BLOCKERS)
        self.assertEqual(self.store.get_goal(self.goal["id"])["status"], "DONE")
        self.assertEqual(summary["stopped_because"], "all jobs complete")

    def test_the_revision_packet_carries_the_exact_blockers(self):
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        prompts = supervisor.provider.prompts_for("BUILD-add-workspace-support-002")
        self.assertEqual(len(prompts), 1)
        for blocker in BLOCKERS:
            self.assertIn(blocker, prompts[0])
        self.assertIn("Fix exactly these, nothing else", prompts[0])

    def test_the_first_attempt_packet_has_no_blockers(self):
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        first = supervisor.provider.prompts_for("BUILD-add-workspace-support-001")[0]
        self.assertNotIn(BLOCKERS[0], first)

    def test_the_rejected_attempt_is_preserved_and_linked(self):
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        relations = {(r["job_id"], r["kind"]) for r in
                     self.store.relations("BUILD-add-workspace-support-001")}
        self.assertIn(("BUILD-add-workspace-support-001", "SUPERSEDED_BY"), relations)
        self.assertIsNotNone(self.store.get_job("BUILD-add-workspace-support-001"))

    def test_a_fresh_reviewer_reviews_the_revision(self):
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        second = self.store.get_job("REVIEWER-add-workspace-support-002")
        self.assertIsNotNone(second)
        self.assertEqual(second["reviews_job_id"], "BUILD-add-workspace-support-002")
        self.assertEqual(second["session_policy"], "fresh")

    def test_the_approver_is_the_reviewer_not_the_builder(self):
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        approvals = [t for t in self.store.transitions("BUILD-add-workspace-support-002")
                     if t["to_status"] == machine.APPROVED]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["actor"], "worker:REVIEWER-add-workspace-support-002")
        work_actors = self.store.work_actors("BUILD-add-workspace-support-002")
        self.assertNotIn(approvals[0]["actor"], work_actors)

    def test_landing_is_a_separate_job_that_runs_after_approval(self):
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        landing = self.store.get_job("LANDING-add-workspace-support-001")
        self.assertEqual(landing["role"], "landing")
        self.assertEqual(landing["lands_job_id"], "BUILD-add-workspace-support-002")
        self.assertEqual(landing["status"], machine.DONE)
        statuses = [t["to_status"] for t in
                    self.store.transitions("BUILD-add-workspace-support-002")]
        self.assertEqual(statuses[statuses.index(machine.APPROVED):],
                         [machine.APPROVED, machine.LANDING_READY, machine.LANDING,
                          machine.VERIFIED, machine.EVALUATED, machine.DONE])

    def test_evaluator_runs_after_verify_and_closes_the_goal(self):
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        evaluator = self.store.get_job("EVALUATOR-add-workspace-support-001")
        self.assertEqual(evaluator["status"], machine.DONE)
        transitions = self.store.transitions("BUILD-add-workspace-support-002")
        verified_at = next(t["id"] for t in transitions if t["to_status"] == machine.VERIFIED)
        evaluated_at = next(t["id"] for t in transitions if t["to_status"] == machine.EVALUATED)
        self.assertLess(verified_at, evaluated_at)

    def test_every_job_produced_a_run_and_a_report(self):
        from devsupervisor import artifacts, metrics
        supervisor = self._supervise()
        supervisor.run(self.project["id"])
        for job_id in ("BUILD-add-workspace-support-002",
                       "REVIEWER-add-workspace-support-002",
                       "LANDING-add-workspace-support-001"):
            self.assertTrue(metrics.runs_for(self.store, job_id), job_id)
            self.assertTrue(artifacts.for_job(self.store, job_id, kind="report"), job_id)


class RevisionCapTests(HarnessTestCase):
    def test_endless_rejection_escalates_instead_of_looping(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add a widget")
        provider = MockProvider(script={"reviewer": reject("still wrong")},
                                default=completed(result_sha="sha"))
        supervisor = Supervisor(self.store, provider=provider)
        supervisor.plan_goal(goal)
        summary = supervisor.run(project["id"])

        builds = [j for j in self.store.list_jobs(project["id"], role="build")]
        self.assertEqual(len(builds), 3)          # original + 2 permitted revisions
        last = builds[-1]
        self.assertEqual(last["status"], machine.WAITING_HUMAN)
        blocked_first = [t["to_status"] for t in self.store.transitions(last["id"])]
        self.assertIn(machine.BLOCKED, blocked_first)
        kinds = [g["kind"] for g in summary["open_gates"]]
        self.assertIn("retries_exhausted", kinds)
        self.assertEqual(summary["stopped_because"], "waiting on a human gate")


class NonBuildProducerTests(HarnessTestCase):
    """Review policy routes work, not the producing role's name."""

    def setUp(self):
        super().setUp()
        self.project = self.make_project()

    def _produce(self, role, review_policy):
        from devsupervisor.providers.mock import MockProvider, completed
        job = self.store.create_job(self.project["id"], "benchmark", role, "a package",
                                    review_policy=review_policy, worktree="/tmp/wt")
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        supervisor = Supervisor(self.store, provider=MockProvider(
            default=completed("built the package")))
        supervisor.advance(self.store.get_job(job["id"]))
        return self.store.get_job(job["id"])

    def test_a_non_build_role_owing_a_review_goes_under_review(self):
        # This is the case that broke a live benchmark lane: role "benchmark"
        # was not in LANDABLE_ROLES, so it walked into APPROVED with an
        # independent review policy still set and the guard refused it.
        for role in ("benchmark", "researcher", "architect"):
            job = self._produce(role, "independent")
            self.assertEqual(job["status"], machine.UNDER_REVIEW, role)

    def test_a_non_build_role_owing_no_review_finishes(self):
        job = self._produce("investigator", "none")
        self.assertEqual(job["status"], machine.DONE)

    def test_a_build_role_owing_no_review_goes_toward_landing(self):
        job = self._produce("build", "none")
        self.assertEqual(job["status"], machine.LANDING_READY)

    def test_a_build_role_owing_a_review_still_goes_under_review(self):
        job = self._produce("build", "independent")
        self.assertEqual(job["status"], machine.UNDER_REVIEW)
