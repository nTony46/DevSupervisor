"""Independent jobs run at the same time; jobs sharing a checkout do not."""

from devsupervisor import parallel
from devsupervisor.providers.mock import MockProvider, completed
from devsupervisor.state import machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase


class ParallelSelectionTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()

    def _ready(self, subject, worktree):
        job = self.store.create_job(self.project["id"], "feature", "reviewer", subject,
                                    worktree=worktree, repo=worktree)
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        return job

    def test_jobs_in_different_worktrees_are_selected_together(self):
        self._ready("lane a", "/tmp/tree-a")
        self._ready("lane b", "/tmp/tree-b")
        selected = parallel.ready_for_parallel(self.store, self.project["id"])
        self.assertEqual(len(selected), 2)

    def test_jobs_sharing_a_worktree_are_not_run_together(self):
        self._ready("lane a", "/tmp/tree-a")
        self._ready("lane b", "/tmp/tree-a")
        selected = parallel.ready_for_parallel(self.store, self.project["id"])
        self.assertEqual(len(selected), 1)


class ParallelDispatchTests(HarnessTestCase):
    def test_both_jobs_advance_and_neither_is_run_twice(self):
        project = self.make_project()
        jobs = []
        for subject, tree in (("lane a", "/tmp/tree-a"), ("lane b", "/tmp/tree-b")):
            job = self.store.create_job(project["id"], "feature", "investigator", subject,
                                        worktree=tree, repo=tree, review_policy="none")
            self.store.transition(job["id"], machine.READY, actor="scheduler")
            jobs.append(job["id"])
        self.store.close()

        def factory(store):
            return Supervisor(store, provider=MockProvider(default=completed("done")))

        results = parallel.dispatch_parallel(project["id"], jobs, factory)

        from devsupervisor.state import Store
        self.store = Store.open()
        self.assertEqual(sorted(results), sorted(jobs))
        for job_id in jobs:
            self.assertIsNone(results[job_id].get("error"), results[job_id])
            self.assertEqual(self.store.get_job(job_id)["status"], machine.DONE)
            runs = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE job_id = ?", (job_id,)).fetchone()
            self.assertEqual(runs["n"], 1)

    def test_a_failing_thread_is_reported_not_swallowed(self):
        project = self.make_project()
        job = self.store.create_job(project["id"], "feature", "investigator", "lane a")
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        self.store.close()

        class Exploding(Supervisor):
            def advance(self, job):
                raise RuntimeError("boom")

        results = parallel.dispatch_parallel(
            project["id"], [job["id"]],
            lambda store: Exploding(store, provider=MockProvider(default=completed())))

        from devsupervisor.state import Store
        self.store = Store.open()
        self.assertIn("boom", results[job["id"]]["error"])
