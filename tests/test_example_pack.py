"""The shipped example pack, and packs that live outside the repository."""

from pathlib import Path

from devsupervisor.errors import PolicyViolation
from devsupervisor.policy import packs, risk
from tests.support import HarnessTestCase


class ExamplePackTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.pack = packs.load("example")

    def test_frozen_benchmark_work_is_critical_and_gated(self):
        self.assertEqual(
            risk.classify("mutate the frozen benchmark baseline", pack=self.pack),
            risk.CRITICAL)
        self.assertEqual(self.pack.gates_for("reseal the candidate package"),
                         ["benchmark_mutation"])

    def test_retrieval_work_is_at_least_high_risk(self):
        for subject in ("tighten exact retrieval routing", "change ranking weights",
                        "add a provenance field to the packet"):
            self.assertEqual(risk.classify(subject, pack=self.pack), risk.HIGH, subject)

    def test_paid_experiments_need_a_budget_gate(self):
        self.assertEqual(self.pack.gates_for("launch the paid A/B run"), ["budget"])

    def test_benchmark_packages_are_protected_paths(self):
        with self.assertRaises(PolicyViolation):
            self.pack.check_paths(["benchmarks/frozen/candidate-3/manifest.json"])
        self.assertTrue(self.pack.check_paths(["crates/retrieval/src/lib.rs"]))

    def test_oracle_data_never_reaches_an_implementer(self):
        with self.assertRaises(PolicyViolation):
            self.pack.check_clean_room("build", ["benchmarks/frozen/candidate-1/oracle.json"])
        self.assertTrue(self.pack.check_clean_room(
            "evaluator", ["benchmarks/frozen/candidate-1/oracle.json"]))

    def test_a_harness_commit_is_not_a_product_sha(self):
        with self.assertRaises(PolicyViolation):
            self.pack.check_product_sha("ba90ab5", ["scripts/harness/eval/run.py"])
        self.assertEqual(
            self.pack.check_product_sha("6ca4ddd", ["crates/app/src/context.rs"]),
            "product")

    def test_mixed_commits_are_reported_as_mixed_not_guessed(self):
        self.assertEqual(
            self.pack.commit_identity(["crates/x/src/lib.rs",
                                       "scripts/harness/eval/run.py"]),
            "mixed")

    def test_the_pack_carries_the_open_decisions_a_repo_cannot_answer(self):
        kinds = {kind for kind, _, _ in self.pack.open_decisions}
        self.assertIn("budget", kinds)
        self.assertIn("benchmark_mutation", kinds)
        self.assertTrue(all(question and context
                            for _, question, context in self.pack.open_decisions))


EXTERNAL_PACK = '''
from devsupervisor.policy.packs import register
from devsupervisor.policy.packs.base import PolicyPack


@register
class LocalPack(PolicyPack):
    name = "local-only"
    description = "A pack that lives beside the runtime root, not in the repo."
    protected_paths = ("secrets/",)
'''


class ExternalPackTests(HarnessTestCase):
    """A real project's pack belongs in $DEVSUPERVISOR_HOME/packs, so the
    repository never has to name the project."""

    def test_a_pack_in_the_runtime_root_loads_by_name(self):
        packs_dir = self.home / "packs"
        packs_dir.mkdir()
        (packs_dir / "local.py").write_text(EXTERNAL_PACK)
        (packs_dir / "test_local.py").write_text("raise AssertionError('not a pack')")
        (packs_dir / "_helper.py").write_text("raise AssertionError('not a pack')")

        self.assertEqual(packs.load_from(packs_dir), ["local-only"])
        self.assertIn("local-only", packs.available())
        pack = packs.load("local-only")
        with self.assertRaises(PolicyViolation):
            pack.check_paths(["secrets/prod.env"])

    def test_a_missing_packs_directory_is_not_an_error(self):
        self.assertEqual(packs.load_from(self.home / "no-such-dir"), [])
