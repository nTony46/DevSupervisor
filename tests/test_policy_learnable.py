"""Learnable policy is versioned and adopted by a named actor, never by itself."""

from devsupervisor.errors import PolicyViolation
from devsupervisor.policy import learnable
from tests.support import HarnessTestCase


class LearnablePolicyTests(HarnessTestCase):
    def test_proposal_is_a_candidate_and_has_no_effect(self):
        policy = learnable.propose(self.store, "decomposition.max_modules", "decomposition",
                                   {"max_modules": 3}, rationale="rejections cluster above 3")
        self.assertEqual(policy["status"], "CANDIDATE")
        self.assertIsNone(learnable.active(self.store, "decomposition.max_modules"))
        self.assertEqual(learnable.effective(self.store, "decomposition.max_modules",
                                             {"max_modules": 99}), {"max_modules": 99})

    def test_adoption_takes_effect_and_records_the_actor(self):
        policy = learnable.propose(self.store, "decomposition.max_modules", "decomposition",
                                   {"max_modules": 3})
        adopted = learnable.adopt(self.store, policy["id"], actor="tony")
        self.assertEqual(adopted["status"], "ADOPTED")
        self.assertEqual(adopted["adopted_by"], "tony")
        self.assertEqual(learnable.effective(self.store, "decomposition.max_modules", {}),
                         {"max_modules": 3})

    def test_versions_supersede_rather_than_overwrite(self):
        first = learnable.propose(self.store, "review_depth.low_docs", "review_depth",
                                  {"reviewer": False})
        learnable.adopt(self.store, first["id"], actor="tony")
        second = learnable.propose(self.store, "review_depth.low_docs", "review_depth",
                                   {"reviewer": False, "qa": False})
        learnable.adopt(self.store, second["id"], actor="tony")
        self.assertEqual(learnable.get(self.store, first["id"])["status"], "SUPERSEDED")
        self.assertEqual(learnable.active(self.store, "review_depth.low_docs")["version"], 2)

    def test_adopting_twice_is_refused(self):
        policy = learnable.propose(self.store, "concurrency.max", "concurrency", {"max": 4})
        learnable.adopt(self.store, policy["id"], actor="tony")
        with self.assertRaises(PolicyViolation):
            learnable.adopt(self.store, policy["id"], actor="tony")

    def test_rejection_keeps_the_record(self):
        policy = learnable.propose(self.store, "concurrency.max", "concurrency", {"max": 40})
        rejected = learnable.reject(self.store, policy["id"], actor="tony", note="too many")
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertIn("too many", rejected["rationale"])
