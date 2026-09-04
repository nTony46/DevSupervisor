"""Every durable entity must round-trip, and dependency readiness must be SQL."""

from devsupervisor.state import machine
from tests.support import HarnessTestCase


class EntityTests(HarnessTestCase):
    def test_all_entities_round_trip(self):
        project = self.make_project(policy_pack="example")
        goal = self.store.create_goal(
            project["id"], "Ship X", acceptance_criteria=["tests green", "docs updated"]
        )
        plan = self.store.create_plan(goal["id"], "feature", rationale="standard feature flow")
        job = self.store.create_job(
            project["id"], "feature", "build", "widget",
            goal_id=goal["id"], plan_id=plan["id"], branch="feat/widget", base_sha="deadbeef",
            acceptance_criteria=["a", "b"], metadata={"lane": "A"},
        )

        self.assertEqual(self.store.get_project(project["id"])["policy_pack"], "example")
        self.assertEqual(self.store.get_goal(goal["id"])["acceptance_criteria"],
                         ["tests green", "docs updated"])
        self.assertEqual(self.store.active_plan(goal["id"])["id"], plan["id"])
        stored = self.store.get_job(job["id"])
        self.assertEqual(stored["acceptance_criteria"], ["a", "b"])
        self.assertEqual(stored["metadata"], {"lane": "A"})
        self.assertEqual(stored["branch"], "feat/widget")

    def test_job_ids_are_stable_and_readable(self):
        project = self.make_project()
        first = self.store.create_job(project["id"], "feature", "build", "provenance line range")
        second = self.store.create_job(project["id"], "feature", "build", "provenance line range")
        self.assertEqual(first["id"], "BUILD-provenance-line-range-001")
        self.assertEqual(second["id"], "BUILD-provenance-line-range-002")

    def test_replanning_supersedes_the_previous_plan(self):
        goal = self.make_goal()
        first = self.store.create_plan(goal["id"], "feature")
        second = self.store.create_plan(goal["id"], "feature", rationale="new evidence")
        self.assertEqual(self.store.active_plan(goal["id"])["id"], second["id"])
        self.assertEqual(second["version"], first["version"] + 1)

    def test_unknown_job_field_is_rejected(self):
        project = self.make_project()
        with self.assertRaises(ValueError):
            self.store.create_job(project["id"], "feature", "build", "x", nonsense=1)


class DependencyTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.first = self.store.create_job(self.project["id"], "feature", "build", "first")
        self.second = self.store.create_job(
            self.project["id"], "feature", "build", "second", depends_on=[self.first["id"]]
        )

    def test_dependency_blocks_readiness(self):
        promoted = [j["id"] for j in self.store.promote_ready(self.project["id"])]
        self.assertEqual(promoted, [self.first["id"]])
        self.assertEqual(self.store.unmet_dependencies(self.second["id"]), [self.first["id"]])

    def test_dependency_unblocks_when_upstream_is_done(self):
        self.store.promote_ready(self.project["id"])
        self.drive(self.first["id"],
                   [machine.DISPATCHED, machine.RUNNING, machine.WORK_COMPLETE],
                   actor="builder")
        self.store.update_job(self.first["id"], review_policy="none", result_sha="sha1")
        self.drive(self.first["id"],
                   [machine.APPROVED, machine.LANDING_READY, machine.LANDING,
                    machine.VERIFIED, machine.EVALUATED, machine.DONE], actor="builder")
        promoted = [j["id"] for j in self.store.promote_ready(self.project["id"])]
        self.assertEqual(promoted, [self.second["id"]])
        self.assertEqual(self.store.unmet_dependencies(self.second["id"]), [])

    def test_self_dependency_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add_dependency(self.first["id"], self.first["id"])


class EventTests(HarnessTestCase):
    def test_repeated_idempotency_key_is_a_noop(self):
        first, created_first = self.store.record_event(
            "result.ingested", {"n": 1}, idempotency_key="run-42"
        )
        second, created_second = self.store.record_event(
            "result.ingested", {"n": 2}, idempotency_key="run-42"
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(second["payload"], {"n": 1})
        self.assertEqual(len(self.store.events(kind="result.ingested")), 1)

    def test_events_without_a_key_always_append(self):
        self.store.record_event("tick")
        self.store.record_event("tick")
        self.assertEqual(len(self.store.events(kind="tick")), 2)


class RelationTests(HarnessTestCase):
    def test_duplicate_results_are_related_not_deleted(self):
        project = self.make_project()
        winner = self.store.create_job(project["id"], "feature", "build", "same task")
        loser = self.store.create_job(project["id"], "feature", "build", "same task")
        self.store.relate(loser["id"], winner["id"], "DUPLICATE", note="same branch and base")
        self.store.transition(loser["id"], machine.DUPLICATE, actor="supervisor")

        self.assertIsNotNone(self.store.get_job(loser["id"]))
        self.assertEqual(self.store.get_job(loser["id"])["status"], machine.DUPLICATE)
        kinds = {r["kind"] for r in self.store.relations(winner["id"])}
        self.assertEqual(kinds, {"DUPLICATE"})
