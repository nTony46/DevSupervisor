"""Bypass is autonomy inside a sandbox. It is never authority."""

import subprocess

from devsupervisor import experiments, landing
from devsupervisor.delegation import DelegationRefused
from devsupervisor.errors import PolicyViolation
from devsupervisor.policy import permissions
from devsupervisor.providers import RunRequest
from devsupervisor.providers.claude_cli import ClaudeCLIProvider
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.state import machine
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase

ISOLATED = "/tmp/disposable-worktree"


class PolicyDefaultTests(HarnessTestCase):
    def test_isolated_worker_roles_default_to_bypass(self):
        for role in ("build", "reviewer", "specialist", "security", "researcher",
                     "investigator", "benchmark", "evaluator", "qa"):
            mode, reason = permissions.resolve(role, worktree=ISOLATED)
            self.assertEqual(mode, permissions.BYPASS, role)
            self.assertIsNone(reason, role)

    def test_privileged_roles_never_default_to_bypass(self):
        for role in ("landing", "operator", "freeze", "supervisor", "planner"):
            mode, _ = permissions.resolve(role, worktree=ISOLATED)
            self.assertEqual(mode, permissions.GUARDED, role)

    def test_a_policy_granting_a_privileged_role_bypass_is_refused(self):
        rogue = {"default": permissions.GUARDED, "roles": {"landing": permissions.BYPASS}}
        with self.assertRaises(PolicyViolation):
            permissions.resolve("landing", worktree=ISOLATED, policy=rogue)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(PolicyViolation):
            permissions.resolve("build", worktree=ISOLATED,
                                policy={"default": "trustMeBro", "roles": {}})


class IsolationTests(HarnessTestCase):
    def test_bypass_is_withheld_without_an_isolated_worktree(self):
        mode, reason = permissions.resolve("build", worktree=None)
        self.assertEqual(mode, permissions.GUARDED)
        self.assertIn("no isolated worktree", reason)

    def test_bypass_is_withheld_in_a_shared_checkout(self):
        mode, reason = permissions.resolve("build", worktree="/repos/main",
                                           shared_paths=("/repos/main",))
        self.assertEqual(mode, permissions.GUARDED)
        self.assertIn("shared checkout", reason)

    def test_a_disposable_worktree_keeps_bypass(self):
        mode, reason = permissions.resolve("build", worktree="/repos/wt-1",
                                           shared_paths=("/repos/main",))
        self.assertEqual(mode, permissions.BYPASS)
        self.assertIsNone(reason)

    def test_the_downgrade_is_recorded_not_silent(self):
        project = self.make_project(repo_path="/repos/main")
        job = self.store.create_job(project["id"], "feature", "build", "widget",
                                    worktree="/repos/main")
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        supervisor = Supervisor(self.store, provider=MockProvider(default=completed()),
                                shared_paths=("/repos/main",))
        supervisor.advance(self.store.get_job(job["id"]))

        events = self.store.events(kind="permission.downgraded")
        self.assertEqual(events[0]["payload"]["job_id"], job["id"])
        self.assertIn("shared checkout", events[0]["payload"]["reason"])
        self.assertEqual(self.store.get_job(job["id"])["permission_mode"],
                         permissions.GUARDED)


class CommandConstructionTests(HarnessTestCase):
    """Command shape only. No paid worker is launched by any test here."""

    def _argv(self, role, mode):
        return ClaudeCLIProvider(budget_usd=10.0, allow_paid=True).build_argv(
            RunRequest(job_id=f"{role.upper()}-x-001", role=role, prompt="p",
                       model="claude-opus-5", effort="high", permission_mode=mode,
                       workdir=ISOLATED))

    def test_an_isolated_worker_receives_the_bypass_flag(self):
        argv = self._argv("build", permissions.BYPASS)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1],
                         "bypassPermissions")

    def test_a_privileged_lander_does_not(self):
        argv = self._argv("landing", permissions.GUARDED)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")

    def test_the_installed_cli_accepts_both_forms(self):
        # Parse-level only: an unknown flag makes the CLI exit before any API call,
        # so this validates syntax against the real binary for free.
        result = subprocess.run(
            ["claude", "--permission-mode", "bypassPermissions",
             "--dangerously-skip-permissions", "--not-a-real-flag", "-p", "x"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("--not-a-real-flag", result.stderr + result.stdout)

    def test_the_installed_cli_rejects_an_invented_mode(self):
        result = subprocess.run(
            ["claude", "--permission-mode", "trustMeBro", "-p", "x"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("invalid", (result.stderr + result.stdout).lower())


class BypassIsNotAuthorityTests(HarnessTestCase):
    """The flag changes what a worker may attempt, not what the harness accepts."""

    def setUp(self):
        super().setUp()
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a")
        self._git("add", "-A")
        self._git("-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "one")
        self.head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        self.project = self.store.create_project("p", str(self.repo))

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def _landing_pair(self, target_status=machine.LANDING_READY, result_sha=None):
        target = self.store.create_job(self.project["id"], "feature", "build", "widget",
                                       repo=str(self.repo), worktree=str(self.repo),
                                       review_policy="independent",
                                       result_sha=result_sha)
        self.drive(target["id"], [machine.READY, machine.DISPATCHED, machine.RUNNING,
                                  machine.WORK_COMPLETE, machine.UNDER_REVIEW],
                   actor="worker:builder")
        if target_status != machine.UNDER_REVIEW:
            self.store.transition(target["id"], machine.APPROVED, actor="worker:reviewer")
            self.store.transition(target["id"], machine.LANDING_READY, actor="supervisor")
        lander = self.store.create_job(self.project["id"], "feature", "landing", "widget",
                                       repo=str(self.repo), worktree=str(self.repo),
                                       lands_job_id=target["id"], review_policy="none")
        return target, lander

    def test_landing_is_refused_without_a_recorded_approval(self):
        target, lander = self._landing_pair(target_status=machine.UNDER_REVIEW,
                                            result_sha=self.head)
        with self.assertRaises(PolicyViolation) as caught:
            landing.preconditions(self.store, lander)
        self.assertIn("not ready to land", str(caught.exception))

    def test_landing_is_refused_when_the_candidate_sha_does_not_exist(self):
        target, lander = self._landing_pair(result_sha="0" * 40)
        with self.assertRaises(PolicyViolation) as caught:
            landing.preconditions(self.store, lander)
        self.assertIn("not present", str(caught.exception))

    def test_a_history_rewrite_is_caught_after_the_fact(self):
        target, lander = self._landing_pair(result_sha=self.head)
        landing.preconditions(self.store, lander, main_ref="main")
        # The worker "lands" by rewriting main onto an unrelated root.
        self._git("checkout", "-q", "--orphan", "rewritten")
        (self.repo / "b.txt").write_text("b")
        self._git("add", "-A")
        self._git("-c", "user.email=t@e.com", "-c", "user.name=T",
                  "commit", "-q", "-m", "rewrite")
        self._git("branch", "-f", "main", "rewritten")
        with self.assertRaises(PolicyViolation) as caught:
            landing.verify(self.store, self.store.get_job(lander["id"]), "whatever")
        self.assertIn("did not put", str(caught.exception))

    def test_a_clean_fast_forward_verifies(self):
        self._git("checkout", "-q", "-b", "feature")
        (self.repo / "c.txt").write_text("c")
        self._git("add", "-A")
        self._git("-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "two")
        candidate = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        self._git("checkout", "-q", "main")
        target, lander = self._landing_pair(result_sha=candidate)
        landing.preconditions(self.store, lander, main_ref="main")
        self._git("merge", "-q", "--ff-only", "feature")
        findings = landing.verify(self.store, self.store.get_job(lander["id"]), candidate)
        self.assertTrue(findings["checked"])
        self.assertEqual(findings["approved_sha"], candidate)

    def test_immutable_rules_still_forbid_force_push_regardless_of_mode(self):
        from devsupervisor.policy import immutable
        for command in ("git push --force origin main", "git reset --hard origin/main"):
            with self.assertRaises(PolicyViolation):
                immutable.check_command(command)


class PairLockPermissionTests(HarnessTestCase):
    def test_arms_cannot_differ_in_permission_mode(self):
        project = self.make_project()
        pair = experiments.create_pair(
            self.store, project["id"], "reuse ceiling",
            {"control": None, "treatment": "context on"},
            risk="HIGH", scope="run the set", worktree="/tmp/arm")
        self.store.update_job(pair["arms"][1]["id"],
                              permission_mode=permissions.GUARDED)
        self.store.update_job(pair["arms"][0]["id"],
                              permission_mode=permissions.BYPASS)
        with self.assertRaises(experiments.ExperimentPairViolation) as caught:
            experiments.check_pair(self.store, self.store.get_job(pair["arms"][0]["id"]))
        self.assertIn("permission_mode", str(caught.exception))

    def test_dispatch_locks_permission_mode_across_arms(self):
        project = self.make_project(repo_path="/repos/main")
        pair = experiments.create_pair(
            self.store, project["id"], "reuse ceiling",
            {"control": None, "treatment": "context on"},
            risk="HIGH", scope="run the set", worktree="/tmp/arm-shared")
        self.store.update_job(pair["arms"][1]["id"], permission_mode="acceptEdits")
        supervisor = Supervisor(self.store, provider=MockProvider(default=completed()),
                                shared_paths=("/repos/main",))
        self.store.transition(pair["arms"][0]["id"], machine.READY, actor="scheduler")
        supervisor.advance(self.store.get_job(pair["arms"][0]["id"]))
        modes = {self.store.get_job(arm["id"])["permission_mode"] for arm in pair["arms"]}
        self.assertEqual(len(modes), 1)


class ChildAuthorizationTests(HarnessTestCase):
    def test_a_worker_cannot_request_bypass_for_its_child(self):
        from devsupervisor import delegation
        project = self.make_project()
        parent = self.store.create_job(project["id"], "feature", "build", "parent",
                                       worktree="/tmp/wt")
        with self.assertRaises(DelegationRefused) as caught:
            delegation.authorize(self.store, parent, [
                {"role": "security", "scope": "check tokens",
                 "permission_mode": permissions.BYPASS}])
        self.assertIn("execution policy", str(caught.exception))

    def test_a_child_gets_its_mode_from_policy_at_dispatch_not_from_the_request(self):
        project = self.make_project(repo_path="/repos/main")
        goal = self.store.create_goal(project["id"], "Add workspace support")
        provider = MockProvider(script={
            "build": completed("built", result_sha="sha", subtask_requests=[
                {"role": "security", "scope": "check the token path"}]),
            "reviewer": approve(), "evaluator": approve()}, default=completed())
        supervisor = Supervisor(self.store, provider=provider,
                                shared_paths=("/repos/main",))
        supervisor.plan_goal(goal, worktree="/tmp/disposable")
        supervisor.run(project["id"])

        child = self.store.list_jobs(project["id"], role="security")[0]
        self.assertEqual(child["metadata"]["authorized_by"], "supervisor")
        self.assertEqual(child["permission_mode"], permissions.BYPASS)


class RunMetadataTests(HarnessTestCase):
    def test_every_run_records_mode_bypass_flag_and_worktree(self):
        project = self.make_project(repo_path="/repos/main")
        goal = self.store.create_goal(project["id"], "Add workspace support")
        supervisor = Supervisor(self.store, provider=MockProvider(
            script={"build": completed("built", result_sha="sha"),
                    "reviewer": approve(), "evaluator": approve()},
            default=completed()), shared_paths=("/repos/main",))
        supervisor.plan_goal(goal, worktree="/tmp/disposable")
        supervisor.run(project["id"])

        rows = self.store.conn.execute("SELECT * FROM runs").fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["permission_mode"], permissions.MODES, row["role"])
            self.assertEqual(bool(row["bypass_permissions"]),
                             row["permission_mode"] == permissions.BYPASS, row["role"])
            self.assertEqual(row["worktree"], "/tmp/disposable", row["role"])
            self.assertTrue(row["model"])
            self.assertEqual(row["effort"], "high")

        bypassed = {row["role"] for row in rows if row["bypass_permissions"]}
        guarded = {row["role"] for row in rows if not row["bypass_permissions"]}
        self.assertIn("build", bypassed)
        self.assertIn("reviewer", bypassed)
        self.assertIn("landing", guarded)
