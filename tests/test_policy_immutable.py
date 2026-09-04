"""The hard rules are constants in source. There is nothing to call to change them."""

from devsupervisor.errors import PolicyViolation
from devsupervisor.policy import immutable, learnable
from tests.support import HarnessTestCase


class ImmutabilityTests(HarnessTestCase):
    def test_rules_are_an_immutable_sequence(self):
        with self.assertRaises((AttributeError, TypeError)):
            immutable.RULES[0] = "anything goes"
        with self.assertRaises(TypeError):
            immutable.FORBIDDEN_COMMANDS["push --force"] = "fine actually"

    def test_module_exposes_no_mutation_api(self):
        setters = [name for name in dir(immutable)
                   if name.startswith(("set_", "add_", "remove_", "update_", "delete_"))]
        self.assertEqual(setters, [])

    def test_forbidden_commands_are_refused(self):
        for command in ("git push --force origin main",
                        "git push -f",
                        "git reset --hard origin/main",
                        "git filter-branch --tree-filter x"):
            with self.assertRaises(PolicyViolation, msg=command):
                immutable.check_command(command)

    def test_ordinary_commands_pass(self):
        for command in ("git push origin feature", "cargo test", "git merge --ff-only abc123"):
            self.assertTrue(immutable.check_command(command))

    def test_landing_without_approval_is_refused(self):
        job = {"id": "BUILD-x-001", "review_policy": "independent"}
        with self.assertRaises(PolicyViolation):
            immutable.check_approval(job, approvals=[])
        self.assertTrue(immutable.check_approval(job, approvals=["REVIEWER-x-001"]))

    def test_learnable_policy_cannot_target_a_safety_rule(self):
        for name in ("immutable.force_push", "safety_review", "hard_rule_7"):
            with self.assertRaises(PolicyViolation, msg=name):
                learnable.propose(self.store, name, "review_depth", {"skip": True})

    def test_unknown_policy_kinds_are_refused(self):
        with self.assertRaises(PolicyViolation):
            learnable.propose(self.store, "reviewer_independence", "safety", {"off": True})

    def test_rules_survive_a_retrospective_that_tries_to_change_them(self):
        before = immutable.rules()
        try:
            learnable.propose(self.store, "immutable.reviewer_independence",
                              "review_depth", {"enabled": False})
        except PolicyViolation:
            pass
        self.assertEqual(immutable.rules(), before)
        self.assertEqual(learnable.list_policies(self.store), [])
