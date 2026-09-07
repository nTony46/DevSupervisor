"""Two independent reviews run in parallel, so one of them always finishes second.

The second verdict used to hit a target that had already left UNDER_REVIEW and
die as an illegal transition. A real campaign lost a security review that way:
the build was revised against the code reviewer's blockers while the security
reviewer's — the more serious set — stayed in the runs table and reached nobody.
"""

from devsupervisor.providers.mock import MockProvider, approve, completed, reject
from devsupervisor.state import machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase


class ParallelReviewVerdictTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.goal = self.store.create_goal(self.project["id"], "Add workspace support")
        self.supervisor = Supervisor(self.store, provider=MockProvider(
            script={"build": completed("built", result_sha="sha")}, default=completed()))
        self.plan = self.supervisor.plan_goal(self.goal)
        self.build_id = "BUILD-add-workspace-support-001"
        for _ in range(6):
            jobs = self.supervisor.scheduler.ready_jobs(self.project["id"], limit=1)
            if not jobs:
                break
            self.supervisor.advance(jobs[0])
            if self.store.get_job(self.build_id)["status"] == machine.UNDER_REVIEW:
                break
        self._add_security_reviewer()

    def _add_security_reviewer(self):
        """The lane under repair ran a code review and a security review together."""
        build = self.store.get_job(self.build_id)
        self.store.create_job(
            self.project["id"], build["job_type"], "specialist", "Security review",
            goal_id=build["goal_id"], reviews_job_id=self.build_id,
            risk=build["risk"], review_policy="none",
            scope="independent security review", repo=build["repo"],
            branch=build["branch"], base_sha=build["base_sha"],
            worktree=build["worktree"], status=machine.READY)

    def _reviewers(self):
        return [job for job in self.store.list_jobs(project_id=self.project["id"])
                if job["reviews_job_id"] == self.build_id
                and job["role"] in ("reviewer", "specialist")]

    def _verdict(self, reviewer, result):
        provider = MockProvider(script={reviewer["role"]: result}, default=result)
        self.supervisor.provider = provider
        self.supervisor.scheduler.provider = provider
        job = self.store.get_job(reviewer["id"])
        if job["status"] == machine.PLANNED:
            job = self.store.transition(job["id"], machine.READY, actor="test",
                                        reason="dependencies satisfied")
        self.supervisor.advance(job)

    def _live_revision(self):
        rows = self.store.conn.execute(
            "SELECT id FROM jobs WHERE revision_of = ?", (self.build_id,)).fetchall()
        live = [self.store.get_job(r["id"]) for r in rows]
        return [j for j in live if j["status"] not in machine.TERMINAL]

    def test_the_second_rejection_merges_its_blockers_into_the_one_revision(self):
        # Arrange: two independent reviewers on one candidate.
        first, second = self._reviewers()[:2]

        # Act: both reject, one after the other.
        self._verdict(first, reject("first blocker"))
        self._verdict(second, reject("second blocker"))

        # Assert: one revision, carrying both sets.
        revisions = self._live_revision()
        self.assertEqual(len(revisions), 1, "a second rejection must not fork the lane")
        self.assertEqual(revisions[0]["blockers"], ["first blocker", "second blocker"])

    def test_a_rejection_after_an_approval_pulls_the_candidate_back(self):
        first, second = self._reviewers()[:2]

        self._verdict(first, approve("looks fine"))
        self.assertEqual(self.store.get_job(self.build_id)["status"], machine.LANDING_READY)

        self._verdict(second, reject("security blocker"))

        build = self.store.get_job(self.build_id)
        self.assertEqual(build["status"], machine.SUPERSEDED,
                         "an approved candidate with a blocking review must not land")
        revisions = self._live_revision()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["blockers"], ["security blocker"])

    def test_an_approval_after_a_rejection_does_not_resurrect_the_candidate(self):
        first, second = self._reviewers()[:2]

        self._verdict(first, reject("real blocker"))
        self._verdict(second, approve("looks fine to me"))

        self.assertEqual(self.store.get_job(self.build_id)["status"], machine.SUPERSEDED)
        revisions = self._live_revision()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["blockers"], ["real blocker"])
        kinds = [e["kind"] for e in self.store.events(job_id=second["id"])]
        self.assertIn("review.non_decisive", kinds)

    def test_no_verdict_is_lost_as_an_illegal_transition(self):
        first, second = self._reviewers()[:2]
        self._verdict(first, reject("first"))
        self._verdict(second, reject("second"))
        for reviewer in (first, second):
            self.assertEqual(self.store.get_job(reviewer["id"])["status"], machine.DONE)
