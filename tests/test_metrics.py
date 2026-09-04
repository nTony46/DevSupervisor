"""Runs and measurements must be recorded even when nobody is reading them."""

from devsupervisor import metrics, taxonomy
from devsupervisor.providers.base import RUN_SUCCEEDED, RunOutcome
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.results import WorkerResult
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase


class RunRecordingTests(HarnessTestCase):
    def test_a_run_records_provider_cost_and_duration(self):
        project = self.make_project()
        job = self.store.create_job(project["id"], "feature", "build", "widget")
        run = metrics.start_run(self.store, job, provider="mock", model="m")
        outcome = RunOutcome(status=RUN_SUCCEEDED, result=WorkerResult(status="COMPLETED"),
                             tokens_in=100, tokens_out=50, cost_usd=0.25, session_id="s1")
        finished = metrics.finish_run(self.store, run["id"], outcome)

        self.assertEqual(finished["status"], RUN_SUCCEEDED)
        self.assertEqual(finished["session_id"], "s1")
        self.assertIsNotNone(finished["duration_s"])
        self.assertEqual([r["value"] for r in metrics.values(self.store, "run.cost_usd")], [0.25])
        self.assertEqual(metrics.total_cost(self.store), 0.25)

    def test_worker_reported_metrics_are_kept(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        provider = MockProvider(script={
            "build": completed("built", result_sha="sha", metrics={"modules_touched": 5}),
            "reviewer": approve(), "evaluator": approve()}, default=completed())
        supervisor = Supervisor(self.store, provider=provider)
        supervisor.plan_goal(goal)
        supervisor.run(project["id"])
        values = [r["value"] for r in metrics.values(self.store, "worker.modules_touched")]
        self.assertEqual(values, [5.0])


class TaxonomyTests(HarnessTestCase):
    def test_blockers_are_classified_into_coarse_buckets(self):
        self.assertEqual(taxonomy.classify_blocker("this is out of scope"), "scope")
        self.assertEqual(taxonomy.classify_blocker("off by one at end of file"), "correctness")
        self.assertEqual(taxonomy.classify_blocker("no test covers this"), "tests")
        self.assertEqual(taxonomy.classify_blocker("leaks a credential"), "safety")
        self.assertEqual(taxonomy.classify_blocker("inscrutable"), "other")

    def test_failures_are_classified(self):
        self.assertEqual(taxonomy.classify_failure("timed out after 900s"), "timeout")
        self.assertEqual(taxonomy.classify_failure("invalid result: missing 'status'"),
                         "contract")
