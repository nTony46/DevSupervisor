"""Leases stop two workers from silently solving one job."""

from datetime import datetime, timezone

from devsupervisor import clock
from devsupervisor.errors import LeaseError
from devsupervisor.state import leases, machine
from tests.support import HarnessTestCase


class LeaseTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        clock.freeze(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
        self.project = self.make_project()
        self.job = self.store.create_job(self.project["id"], "feature", "build", "widget")

    def test_second_worker_cannot_take_a_leased_job(self):
        leases.acquire(self.store, self.job["id"], owner="worker-1", ttl_seconds=600)
        with self.assertRaises(LeaseError):
            leases.acquire(self.store, self.job["id"], owner="worker-2")

    def test_same_owner_reacquiring_is_idempotent(self):
        token = leases.acquire(self.store, self.job["id"], owner="worker-1")
        self.assertEqual(leases.acquire(self.store, self.job["id"], owner="worker-1"), token)

    def test_released_lease_can_be_retaken(self):
        token = leases.acquire(self.store, self.job["id"], owner="worker-1")
        leases.release(self.store, self.job["id"], token)
        leases.acquire(self.store, self.job["id"], owner="worker-2")

    def test_releasing_with_the_wrong_token_is_refused(self):
        leases.acquire(self.store, self.job["id"], owner="worker-1")
        with self.assertRaises(LeaseError):
            leases.release(self.store, self.job["id"], token="not-mine")

    def test_expired_lease_is_reclaimed_and_the_job_returns_to_ready(self):
        self.store.transition(self.job["id"], machine.READY, actor="scheduler")
        self.store.transition(self.job["id"], machine.DISPATCHED, actor="scheduler")
        self.store.transition(self.job["id"], machine.RUNNING, actor="worker-1")
        leases.acquire(self.store, self.job["id"], owner="worker-1", ttl_seconds=60)

        self.assertEqual(leases.reclaim_expired(self.store), [])
        clock.advance(61)
        self.assertEqual(leases.reclaim_expired(self.store), [self.job["id"]])

        self.assertEqual(self.store.get_job(self.job["id"])["status"], machine.READY)
        self.assertIsNone(leases.holder(self.store, self.job["id"]))

    def test_reclaim_is_auditable(self):
        leases.acquire(self.store, self.job["id"], owner="worker-1", ttl_seconds=10)
        clock.advance(20)
        leases.reclaim_expired(self.store)
        events = self.store.events(kind="lease.reclaimed")
        self.assertEqual(events[0]["payload"]["previous_owner"], "worker-1")


class SupervisorLockTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        clock.freeze(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))

    def test_only_one_supervisor_may_hold_the_lock(self):
        leases.acquire_supervisor_lock(self.store, owner="supervisor-a", ttl_seconds=300)
        with self.assertRaises(LeaseError):
            leases.acquire_supervisor_lock(self.store, owner="supervisor-b")

    def test_stale_lock_is_takeable(self):
        leases.acquire_supervisor_lock(self.store, owner="supervisor-a", ttl_seconds=60)
        clock.advance(61)
        self.assertTrue(leases.supervisor_lock_status(self.store)["expired"])
        leases.acquire_supervisor_lock(self.store, owner="supervisor-b")
        self.assertEqual(leases.supervisor_lock_status(self.store)["owner"], "supervisor-b")

    def test_release_frees_the_lock(self):
        leases.acquire_supervisor_lock(self.store, owner="supervisor-a")
        leases.release_supervisor_lock(self.store, owner="supervisor-a")
        self.assertIsNone(leases.supervisor_lock_status(self.store))
