"""An A/B is only evidence if the arms differ in exactly one thing."""

from devsupervisor import experiments
from devsupervisor.experiments import ExperimentPairViolation
from devsupervisor.providers.mock import MockProvider, completed
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase

SHARED = dict(
    risk="HIGH", repo="/repo", base_sha="6ca4ddd", branch="eval/harness",
    scope="Run the Benchmark task set and record outcome metrics.",
    non_goals="Do not tune retrieval. Do not touch the frozen packages.",
    output_contract="raw results artifact",
    review_policy="independent",
    metadata={"timeout_s": 1800, "tools": ["Read", "Bash"], "max_budget_usd": 20.0},
)


class PairCreationTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.goal = self.store.create_goal(self.project["id"], "Measure reuse ceiling")
        self.pair = experiments.create_pair(
            self.store, self.project["id"], "reuse ceiling",
            {"control": None, "treatment": "Example context enabled"},
            goal_id=self.goal["id"], job_type="experiment", **SHARED)

    def test_both_arms_share_every_locked_field(self):
        signatures = [experiments.locked_signature(arm) for arm in self.pair["arms"]]
        self.assertEqual(signatures[0], signatures[1])
        self.assertEqual(experiments.diff_pair(self.store, self.pair["pair_id"]), {})

    def test_the_treatment_is_the_only_declared_difference(self):
        described = experiments.describe(self.store, self.pair["pair_id"])
        treatments = {arm["arm"]: arm["treatment"] for arm in described["arms"]}
        self.assertEqual(treatments, {"control": None,
                                      "treatment": "Example context enabled"})
        self.assertEqual(described["differences"], {})

    def test_a_pair_needs_at_least_two_arms(self):
        with self.assertRaises(ValueError):
            experiments.create_pair(self.store, self.project["id"], "solo",
                                    {"control": None}, **SHARED)


class PairLockTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.goal = self.store.create_goal(self.project["id"], "Measure reuse ceiling")
        self.pair = experiments.create_pair(
            self.store, self.project["id"], "reuse ceiling",
            {"control": None, "treatment": "context enabled"},
            goal_id=self.goal["id"], **SHARED)
        self.control, self.treatment = self.pair["arms"]

    def _assert_divergence_blocks(self, field):
        with self.assertRaises(ExperimentPairViolation) as caught:
            experiments.check_pair(self.store, self.store.get_job(self.control["id"]))
        self.assertIn(field, str(caught.exception))

    def test_arms_cannot_diverge_in_model(self):
        self.store.update_job(self.treatment["id"], model="claude-sonnet-5")
        self._assert_divergence_blocks("model")

    def test_arms_cannot_diverge_in_thinking_effort(self):
        self.store.update_job(self.control["id"], effort="high")
        self.store.update_job(self.treatment["id"], effort="low")
        self._assert_divergence_blocks("effort")

    def test_arms_cannot_diverge_in_base_commit(self):
        self.store.update_job(self.treatment["id"], base_sha="deadbeef")
        self._assert_divergence_blocks("base_sha")

    def test_arms_cannot_diverge_in_the_task_body(self):
        self.store.update_job(self.treatment["id"], scope="Run the task set, but faster.")
        self._assert_divergence_blocks("scope")

    def test_arms_cannot_diverge_in_budget_timeout_or_tools(self):
        for key, value, field in (("max_budget_usd", 99.0, "metadata.max_budget_usd"),
                                  ("timeout_s", 60, "metadata.timeout_s"),
                                  ("tools", ["Read"], "metadata.tools")):
            job = self.store.get_job(self.treatment["id"])
            self.store.update_job(self.treatment["id"],
                                  metadata=dict(job["metadata"], **{key: value}))
            self._assert_divergence_blocks(field)
            self.store.update_job(self.treatment["id"], metadata=job["metadata"])

    def test_a_matched_pair_dispatches_normally(self):
        self.assertTrue(experiments.check_pair(
            self.store, self.store.get_job(self.control["id"])))

    def test_a_job_outside_any_experiment_is_unaffected(self):
        ordinary = self.store.create_job(self.project["id"], "feature", "build", "widget")
        self.assertTrue(experiments.check_pair(self.store, ordinary))


class PairLockAtDispatchTests(HarnessTestCase):
    def test_a_diverged_arm_is_refused_before_anything_is_spent(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Measure reuse ceiling")
        pair = experiments.create_pair(
            self.store, project["id"], "reuse ceiling",
            {"control": None, "treatment": "context enabled"},
            goal_id=goal["id"], **SHARED)
        # Diverge a field the router does not own, so it survives to the check.
        self.store.update_job(pair["arms"][1]["id"], base_sha="deadbeef")

        provider = MockProvider(default=completed())
        supervisor = Supervisor(self.store, provider=provider)
        self.store.transition(pair["arms"][0]["id"], "READY", actor="scheduler")
        with self.assertRaises(ExperimentPairViolation):
            supervisor.advance(self.store.get_job(pair["arms"][0]["id"]))
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM runs")
                         .fetchone()["n"], 0)

    def test_a_hand_edited_arm_model_is_corrected_and_the_correction_recorded(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Measure reuse ceiling")
        pair = experiments.create_pair(
            self.store, project["id"], "reuse ceiling",
            {"control": None, "treatment": "context enabled"},
            goal_id=goal["id"], **SHARED)
        self.store.update_job(pair["arms"][1]["id"], model="claude-haiku-4-5",
                              effort="low")

        supervisor = Supervisor(self.store, provider=MockProvider(default=completed()))
        self.store.transition(pair["arms"][0]["id"], "READY", actor="scheduler")
        supervisor.advance(self.store.get_job(pair["arms"][0]["id"]))

        # Routing belongs to policy, not to whoever edited the row. Both arms end
        # up on the routed configuration, and the correction leaves a trace.
        efforts = {self.store.get_job(arm["id"])["effort"] for arm in pair["arms"]}
        models = {self.store.get_job(arm["id"])["model"] for arm in pair["arms"]}
        self.assertEqual(efforts, {"high"})
        self.assertEqual(len(models), 1)
        self.assertTrue(self.store.events(kind="experiment.routing_locked"))

    def test_routing_keeps_arms_identical_because_role_decides_the_model(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Measure reuse ceiling")
        pair = experiments.create_pair(
            self.store, project["id"], "reuse ceiling",
            {"control": None, "treatment": "context enabled"},
            goal_id=goal["id"], **SHARED)
        supervisor = Supervisor(self.store, provider=MockProvider(default=completed()))
        for arm in pair["arms"]:
            self.store.transition(arm["id"], "READY", actor="scheduler")
            supervisor.advance(self.store.get_job(arm["id"]))
        models = {self.store.get_job(arm["id"])["model"] for arm in pair["arms"]}
        efforts = {self.store.get_job(arm["id"])["effort"] for arm in pair["arms"]}
        self.assertEqual(len(models), 1)
        self.assertEqual(efforts, {"high"})
