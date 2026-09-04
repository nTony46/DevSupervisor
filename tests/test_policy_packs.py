"""The core is project-agnostic; project rules live only in packs."""

import subprocess

from devsupervisor.errors import NotFound, PolicyViolation
from devsupervisor.policy import packs
from devsupervisor.policy.packs import PolicyPack
from tests.support import ROOT, HarnessTestCase

PROJECT_NAMES = ("example", "otherapp", "benchmark", "another")


class FrozenDataPack(PolicyPack):
    """A pack invented by this test — proof that packs are pluggable."""

    name = "frozen-data"
    description = "Benchmark packages are frozen and invisible to implementers."
    protected_paths = ("benchmarks/frozen/",)
    clean_room_roles = ("build", "architect")
    clean_room_paths = ("benchmarks/frozen/oracle", "held-out/")
    gate_triggers = (("benchmark_mutation", ("frozen benchmark", "baseline")),)

    def risk_override(self, text="", paths=(), risk=None):
        if "benchmark" in (text or "").lower():
            return "HIGH"
        return None


class CoreAgnosticismTests(HarnessTestCase):
    def test_core_has_no_project_references(self):
        result = subprocess.run(
            ["grep", "-rniE", "|".join(PROJECT_NAMES), str(ROOT / "devsupervisor")],
            capture_output=True, text=True)
        offending = [line for line in result.stdout.splitlines()
                     if "/policy/packs/" not in line]
        self.assertEqual(offending, [], "project names leaked into the generic core")

    def test_example_rules_live_in_a_pack(self):
        self.assertIn("example", packs.available())
        pack = packs.load("example")
        self.assertEqual(pack.name, "example")

    def test_unknown_pack_fails_loudly(self):
        with self.assertRaises(NotFound):
            packs.load("no-such-project")

    def test_default_pack_is_permissive_and_knows_nothing(self):
        pack = packs.load(None)
        self.assertEqual(pack.protected_paths, ())
        self.assertIsNone(pack.risk_override(text="anything"))


class PackBehaviourTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.pack = FrozenDataPack()

    def test_protected_paths_need_a_gate(self):
        with self.assertRaises(PolicyViolation):
            self.pack.check_paths(["benchmarks/frozen/candidate3/manifest.json"])
        self.assertTrue(self.pack.check_paths(["src/main.rs"]))

    def test_an_approved_gate_releases_the_protected_path(self):
        self.assertTrue(self.pack.check_paths(
            ["benchmarks/frozen/candidate3/manifest.json"], has_gate_approval=True))

    def test_clean_room_keeps_oracle_data_out_of_implementers(self):
        with self.assertRaises(PolicyViolation):
            self.pack.check_clean_room("build", ["benchmarks/frozen/oracle/answers.json"])
        with self.assertRaises(PolicyViolation):
            self.pack.check_clean_room("build", ["held-out/pairs.json"])

    def test_reviewers_are_not_subject_to_the_implementer_clean_room(self):
        self.assertTrue(self.pack.check_clean_room(
            "evaluator", ["benchmarks/frozen/oracle/answers.json"]))

    def test_pack_can_raise_the_risk_floor(self):
        from devsupervisor.policy import risk
        self.assertEqual(risk.classify("add a benchmark runner", pack=self.pack), risk.HIGH)

    def test_pack_gate_triggers_are_reported(self):
        self.assertEqual(self.pack.gates_for("mutate the frozen benchmark baseline"),
                         ["benchmark_mutation"])
        self.assertEqual(self.pack.gates_for("rename a variable"), [])

    def test_pack_renders_its_rules_for_a_packet(self):
        rendered = self.pack.render()
        self.assertIn("frozen-data", rendered)
        self.assertIn("benchmarks/frozen/", rendered)
