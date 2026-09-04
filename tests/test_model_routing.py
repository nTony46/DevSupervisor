"""Routing is policy, resolved from what this machine supports, floored for
critical roles, and recorded on every run."""

import json

from devsupervisor import metrics
from devsupervisor.errors import PolicyViolation
from devsupervisor.policy import immutable, learnable
from devsupervisor.policy.routing import DEFAULT_POLICY, POLICY_NAME, ROUTED_ROLES, ModelRouter
from devsupervisor.providers import discovery
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase

SETTINGS_WITH_OPUS = {
    "model": "opus",
    "effortLevel": "high",
    "modelSettings": {"claude-opus-5": {"effortLevel": "high"}},
}


class ResolutionTests(HarnessTestCase):
    def _settings(self, payload):
        path = self.home / "settings.json"
        path.write_text(json.dumps(payload))
        return path

    def test_a_concrete_opus_id_is_read_from_local_configuration(self):
        resolution = discovery.resolve_opus(
            binary="definitely-not-installed",
            settings_path=self._settings(SETTINGS_WITH_OPUS), env={})
        self.assertEqual(resolution.model_id, "claude-opus-5")
        self.assertTrue(resolution.concrete)
        self.assertIn("modelSettings", resolution.model_source)
        self.assertEqual(resolution.effort, "high")

    def test_no_marketing_string_is_hardcoded_in_the_router(self):
        source = (self.home.parents and None)
        from pathlib import Path
        text = Path(ModelRouter.__module__.replace(".", "/") + ".py")
        body = (text if text.exists() else
                Path("devsupervisor/policy/routing.py")).read_text()
        self.assertNotIn("claude-opus-5", body)
        self.assertNotIn("claude-sonnet", body)

    def test_an_alias_is_used_when_no_concrete_id_is_discoverable(self):
        resolution = discovery.resolve_opus(
            binary="definitely-not-installed", settings_path=self.home / "absent.json",
            env={})
        self.assertEqual(resolution.model_id, "opus")
        self.assertFalse(resolution.concrete)
        self.assertTrue(any("alias" in note for note in resolution.notes))

    def test_an_environment_override_wins_and_is_recorded(self):
        resolution = discovery.resolve_opus(
            binary="definitely-not-installed", settings_path=self._settings(SETTINGS_WITH_OPUS),
            env={discovery.ENV_MODEL_OVERRIDE: "claude-opus-9"})
        self.assertEqual(resolution.model_id, "claude-opus-9")
        self.assertIn("override", resolution.model_source)

    def test_a_configured_effort_below_the_floor_is_raised_not_accepted(self):
        resolution = discovery.resolve_opus(
            binary="definitely-not-installed",
            settings_path=self._settings({"modelSettings": {"claude-opus-5":
                                                            {"effortLevel": "low"}}}),
            env={}, minimum_effort="high")
        self.assertEqual(resolution.effort, "high")
        self.assertTrue(any("below the policy floor" in note for note in resolution.notes))

    def test_a_stronger_configured_effort_is_kept(self):
        resolution = discovery.resolve_opus(
            binary="definitely-not-installed",
            settings_path=self._settings({"effortLevel": "max"}), env={},
            minimum_effort="high")
        self.assertEqual(resolution.effort, "max")

    def test_an_unsupported_effort_falls_back_rather_than_being_sent(self):
        resolution = discovery.resolve_opus(
            binary="definitely-not-installed",
            settings_path=self._settings({"effortLevel": "turbo"}), env={})
        self.assertEqual(resolution.effort, "high")
        self.assertTrue(any("not one of" in note for note in resolution.notes))


class InitialPolicyTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.resolution = discovery.resolve_opus(
            binary="definitely-not-installed",
            settings_path=self._write_settings(), env={})
        self.router = ModelRouter(self.store, resolution=self.resolution)

    def _write_settings(self):
        path = self.home / "settings.json"
        path.write_text(json.dumps(SETTINGS_WITH_OPUS))
        return path

    def test_every_role_resolves_to_opus_at_high_thinking(self):
        for decision in self.router.table():
            self.assertEqual(decision.tier, "opus", decision.role)
            self.assertEqual(decision.model_id, "claude-opus-5", decision.role)
            self.assertEqual(decision.effort, "high", decision.role)

    def test_mechanical_roles_are_not_downgraded_in_the_opening_baseline(self):
        for role in ("landing", "qa", "operator"):
            self.assertEqual(self.router.route(role).effort, "high")

    def test_no_role_is_given_a_fallback_model(self):
        for decision in self.router.table():
            self.assertIsNone(decision.fallback_model, decision.role)

    def test_every_named_reasoning_role_is_routed(self):
        for role in ("supervisor", "planner", "architect", "build", "reviewer",
                     "specialist", "security", "benchmark", "evaluator", "researcher",
                     "investigator", "qa", "landing"):
            self.assertIn(role, ROUTED_ROLES)

    def test_an_unknown_role_falls_back_to_the_strongest_setting(self):
        decision = self.router.route("some-future-role")
        self.assertEqual(decision.tier, "opus")
        self.assertEqual(decision.effort, "high")

    def test_the_resolution_source_is_carried_into_the_decision(self):
        self.assertIn("modelSettings", self.router.route("reviewer").resolution_source)


class CriticalRoleFloorTests(HarnessTestCase):
    def test_a_downgraded_critical_role_is_refused_at_proposal_time(self):
        with self.assertRaises(PolicyViolation):
            learnable.propose(self.store, "model_routing.cheap", "model_choice",
                              {"default": {"tier": "opus", "effort": "high"},
                               "roles": {"reviewer": {"tier": "sonnet"}}})
        self.assertEqual(learnable.list_policies(self.store), [])

    def test_lowering_effort_for_a_critical_role_is_refused(self):
        with self.assertRaises(PolicyViolation):
            learnable.propose(self.store, "model_routing.cheap", "model_choice",
                              {"default": {"tier": "opus", "effort": "high"},
                               "roles": {"evaluator": {"effort": "low"}}})

    def test_a_cheap_default_cannot_sneak_past_by_omitting_critical_roles(self):
        with self.assertRaises(PolicyViolation):
            learnable.propose(self.store, "model_routing.cheap", "model_choice",
                              {"default": {"tier": "haiku", "effort": "low"}, "roles": {}})

    def test_a_fallback_model_on_a_critical_role_is_refused(self):
        with self.assertRaises(PolicyViolation):
            learnable.propose(self.store, "model_routing.fallback", "model_choice",
                              {"default": {"tier": "opus", "effort": "high"},
                               "roles": {"supervisor": {"fallback_model": "sonnet"}}})

    def test_downgrading_a_non_critical_role_is_allowed_as_a_candidate(self):
        policy = learnable.propose(
            self.store, "model_routing.cheap_docs", "model_choice",
            {"default": {"tier": "opus", "effort": "high"},
             "roles": {"investigator": {"tier": "sonnet", "effort": "medium"}}},
            rationale="observed: investigation jobs never produce blockers")
        self.assertEqual(policy["status"], "CANDIDATE")
        # A candidate has no effect until it is adopted.
        self.assertEqual(ModelRouter(self.store).route("investigator").tier, "opus")

    def test_the_floor_is_enforced_at_use_not_only_at_proposal(self):
        rogue = {"default": {"tier": "opus", "effort": "high"},
                 "roles": {"reviewer": {"tier": "haiku", "effort": "low"}}}
        router = ModelRouter(self.store, policy=rogue)
        with self.assertRaises(PolicyViolation):
            router.route("reviewer")
        self.assertEqual(router.route("build").tier, "opus")

    def test_the_critical_role_set_covers_judgment_and_eval_integrity(self):
        for role in ("supervisor", "reviewer", "specialist", "security", "evaluator",
                     "benchmark"):
            self.assertIn(role, immutable.CRITICAL_ROLES)

    def test_a_retrospective_cannot_reach_the_routing_floor(self):
        before = immutable.ROUTING_FLOOR["effort"]
        with self.assertRaises(PolicyViolation):
            learnable.propose(self.store, "immutable.routing_floor", "model_choice",
                              {"default": {"tier": "haiku", "effort": "low"}})
        self.assertEqual(immutable.ROUTING_FLOOR["effort"], before)
        with self.assertRaises((TypeError, AttributeError)):
            immutable.ROUTING_FLOOR["effort"] = "low"


class RunRecordTests(HarnessTestCase):
    def test_model_and_thinking_are_persisted_on_every_run(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        provider = MockProvider(script={"build": completed("built", result_sha="sha"),
                                        "reviewer": approve("correct"),
                                        "evaluator": approve("met")},
                                default=completed())
        supervisor = Supervisor(self.store, provider=provider)
        supervisor.plan_goal(goal)
        supervisor.run(project["id"])

        rows = self.store.conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["model"], row["role"])
            self.assertEqual(row["effort"], "high", row["role"])
            self.assertTrue(row["routing_source"], row["role"])
            self.assertEqual(row["provider"], "mock")
            self.assertIsNotNone(row["duration_s"])
            self.assertIsNotNone(row["tokens_in"])
            self.assertIsNotNone(row["cost_usd"])
            self.assertGreaterEqual(row["attempt"], 1)
            self.assertEqual(row["model_resolved"], row["model"])

    def test_review_outcome_is_recorded_against_the_reviewer_run(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        supervisor = Supervisor(self.store, provider=MockProvider(
            script={"build": completed("built", result_sha="sha"),
                    "reviewer": approve("correct"), "evaluator": approve("met")},
            default=completed()))
        supervisor.plan_goal(goal)
        supervisor.run(project["id"])
        reviewer_runs = metrics.runs_for(self.store, "REVIEWER-add-workspace-support-001")
        self.assertEqual([r["review_outcome"] for r in reviewer_runs], ["APPROVE"])

    def test_the_job_carries_the_routing_it_was_dispatched_with(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        supervisor = Supervisor(self.store, provider=MockProvider(default=completed()))
        supervisor.plan_goal(goal)
        job = supervisor.scheduler.ready_jobs(project["id"], limit=1)[0]
        supervisor.advance(job)
        stored = self.store.get_job(job["id"])
        self.assertEqual(stored["effort"], "high")
        self.assertTrue(stored["model"])

    def test_the_worker_is_actually_asked_for_that_model_and_effort(self):
        project = self.make_project()
        goal = self.store.create_goal(project["id"], "Add workspace support")
        provider = MockProvider(default=completed())
        supervisor = Supervisor(self.store, provider=provider)
        supervisor.plan_goal(goal)
        supervisor.advance(supervisor.scheduler.ready_jobs(project["id"], limit=1)[0])
        self.assertEqual(provider.calls[0].effort, "high")
        self.assertTrue(provider.calls[0].model)
        self.assertIsNone(provider.calls[0].fallback_model)


class ClaudeArgvRoutingTests(HarnessTestCase):
    def test_model_and_effort_reach_the_command_line(self):
        from devsupervisor.providers import RunRequest
        from devsupervisor.providers.claude_cli import ClaudeCLIProvider
        provider = ClaudeCLIProvider(budget_usd=10.0, allow_paid=True)
        argv = provider.build_argv(RunRequest(
            job_id="BUILD-x-001", role="build", prompt="p", model="claude-opus-5",
            effort="high", max_budget_usd=5.0, tools=("Read", "Bash")))
        self.assertIn("--model", argv)
        self.assertIn("claude-opus-5", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "5.0")
        self.assertNotIn("--fallback-model", argv)

    def test_the_concrete_model_is_read_back_from_the_provider_envelope(self):
        from devsupervisor.providers.claude_cli import ClaudeCLIProvider
        self.assertEqual(
            ClaudeCLIProvider.resolved_model({"modelUsage": {"claude-opus-5": {}}}),
            "claude-opus-5")
        self.assertEqual(ClaudeCLIProvider.resolved_model({"model": "claude-opus-5"}),
                         "claude-opus-5")
        self.assertIsNone(ClaudeCLIProvider.resolved_model({}))
