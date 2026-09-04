"""A goal becomes a graph, and the graph's shape is the risk level's doing."""

from devsupervisor.planner import Planner, infer_workflow, workflows
from devsupervisor.policy import risk
from devsupervisor.state import machine
from tests.support import HarnessTestCase


class WorkflowInferenceTests(HarnessTestCase):
    def test_words_choose_the_template(self):
        self.assertEqual(infer_workflow("Fix the crash on empty query"), workflows.BUG)
        self.assertEqual(infer_workflow("Refactor the retrieval module"), workflows.REFACTOR)
        self.assertEqual(infer_workflow("Migrate storage to sqlite"), workflows.MIGRATION)
        self.assertEqual(infer_workflow("Research options for embeddings"), workflows.RESEARCH)
        self.assertEqual(infer_workflow("Run an A/B experiment"), workflows.EXPERIMENT)
        self.assertEqual(infer_workflow("Something unclassifiable"), workflows.FEATURE)


class FeaturePlanTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()

    def _plan(self, title, **kwargs):
        goal = self.store.create_goal(self.project["id"], title, **kwargs)
        return Planner(self.store).plan(goal)

    def test_feature_goal_creates_a_valid_dag(self):
        result = self._plan("Add multi-repository workspace support")
        roles = [job["role"] for job in result["jobs"]]
        self.assertEqual(result["workflow"], workflows.FEATURE)
        self.assertEqual(roles,
                         ["investigator", "planner", "build", "reviewer", "landing", "evaluator"])

        by_role = {job["role"]: job for job in result["jobs"]}
        self.assertEqual(self.store.dependencies(by_role["build"]["id"]),
                         [by_role["planner"]["id"]])
        self.assertIn(by_role["build"]["id"],
                      self.store.dependencies(by_role["reviewer"]["id"]))
        self.assertIn(by_role["reviewer"]["id"],
                      self.store.dependencies(by_role["landing"]["id"]))
        self.assertEqual(self.store.dependencies(by_role["evaluator"]["id"]),
                         [by_role["landing"]["id"]])

    def test_only_the_first_step_is_ready_at_first(self):
        result = self._plan("Add multi-repository workspace support")
        promoted = self.store.promote_ready(self.project["id"])
        self.assertEqual([j["role"] for j in promoted], ["investigator"])

    def test_high_risk_adds_design_and_a_specialist_review(self):
        result = self._plan("Add authentication and permission checks to the search API")
        roles = [job["role"] for job in result["jobs"]]
        self.assertEqual(result["risk"], risk.HIGH)
        self.assertIn("architect", roles)
        self.assertIn("specialist", roles)
        build = next(j for j in result["jobs"] if j["role"] == "build")
        self.assertEqual(build["review_policy"], "independent+specialist")
        specialist = next(j for j in result["jobs"] if j["role"] == "specialist")
        self.assertEqual(specialist["metadata"]["specialists"], ["security"])

    def test_low_risk_docs_work_skips_the_review_scaffolding(self):
        result = self._plan("Fix a typo in the README", acceptance_criteria=["reads correctly"])
        roles = [job["role"] for job in result["jobs"]]
        self.assertEqual(result["risk"], risk.LOW)
        self.assertNotIn("reviewer", roles)
        self.assertNotIn("investigator", roles)
        build = next(j for j in result["jobs"] if j["role"] == "build")
        self.assertEqual(build["review_policy"], "none")

    def test_builder_review_policy_forbids_self_approval_at_medium_risk(self):
        result = self._plan("Add multi-repository workspace support")
        build = next(j for j in result["jobs"] if j["role"] == "build")
        self.assertNotEqual(build["review_policy"], "none")

    def test_reviewers_get_a_fresh_session_by_default(self):
        result = self._plan("Add multi-repository workspace support")
        by_role = {job["role"]: job for job in result["jobs"]}
        self.assertEqual(by_role["reviewer"]["session_policy"], "fresh")
        self.assertEqual(by_role["build"]["session_policy"], "reuse")

    def test_replanning_supersedes_without_destroying_the_old_jobs(self):
        goal = self.store.create_goal(self.project["id"], "Add a widget")
        first = Planner(self.store).plan(goal)
        second = Planner(self.store).plan(goal, rationale="new evidence")
        self.assertEqual(self.store.active_plan(goal["id"])["id"], second["plan"]["id"])
        self.assertIsNotNone(self.store.get_job(first["jobs"][0]["id"]))


class CriticalRiskTests(HarnessTestCase):
    def test_critical_work_is_parked_behind_a_human_gate(self):
        project = self.make_project()
        goal = self.store.create_goal(
            project["id"], "Mutate the frozen benchmark baseline for candidate 3")
        result = Planner(self.store).plan(goal)
        self.assertEqual(result["risk"], risk.CRITICAL)
        self.assertTrue(result["gates"])
        build = next(j for j in result["jobs"] if j["role"] == "build")
        self.assertEqual(self.store.get_job(build["id"])["status"], machine.WAITING_HUMAN)
        # And it stays parked: readiness promotion cannot rescue it.
        promoted = [j["id"] for j in self.store.promote_ready(project["id"])]
        self.assertNotIn(build["id"], promoted)
