"""The core is project-agnostic, proven on a repository it knows nothing about."""

import shutil
import subprocess

from devsupervisor import config, gitfacts
from devsupervisor.cli import main
from devsupervisor.providers.mock import MockProvider, approve, completed
from devsupervisor.state import machine
from devsupervisor.supervisor import Supervisor
from tests.support import ROOT, HarnessTestCase

FIXTURE = ROOT / "examples" / "sample_project"


class SampleProjectTests(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.home / "sample-repo"
        shutil.copytree(FIXTURE, self.repo)
        for argv in (["init", "-q", "-b", "main"],
                     ["add", "-A"],
                     ["-c", "user.email=t@example.com", "-c", "user.name=T",
                      "commit", "-q", "-m", "initial"]):
            subprocess.run(["git", "-C", str(self.repo), *argv], check=True,
                           capture_output=True)
        self.project = self.store.create_project("sample", str(self.repo))
        config.ensure_project_dirs(self.project["id"])

    def test_the_harness_reads_real_git_facts_from_an_unknown_repo(self):
        facts = gitfacts.facts(self.repo)
        self.assertTrue(facts["is_git"])
        self.assertEqual(facts["branch"], "main")
        self.assertTrue(facts["clean"])

    def test_a_bug_goal_runs_the_whole_loop_on_a_foreign_project(self):
        goal = self.store.create_goal(
            self.project["id"], "Fix the crash when averaging an empty list",
            acceptance_criteria=["average([]) does not raise"])
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        provider = MockProvider(script={
            "build": completed("returned 0.0 for an empty sequence",
                               result_sha=head,
                               metrics={"modules_touched": 1},
                               evidence=["python -m unittest discover: 3 passed"]),
            "reviewer": approve("regression test fails without the fix"),
            "evaluator": approve("average([]) returns 0.0"),
        }, default=completed())
        supervisor = Supervisor(self.store, provider=provider,
                                repo_facts=gitfacts.facts(self.repo))
        result = supervisor.plan_goal(goal, repo=str(self.repo))
        self.assertEqual(result["workflow"], "bug")

        summary = supervisor.run(self.project["id"])
        build = next(job for job in self.store.list_jobs(self.project["id"], role="build"))
        self.assertEqual(build["status"], machine.DONE)
        self.assertEqual(self.store.get_goal(goal["id"])["status"], "DONE")
        self.assertEqual(summary["stopped_because"], "all jobs complete")

    def test_the_packet_names_the_foreign_repository_not_a_builtin_one(self):
        goal = self.store.create_goal(self.project["id"], "Add a median helper")
        supervisor = Supervisor(self.store, repo_facts=gitfacts.facts(self.repo))
        supervisor.plan_goal(goal, repo=str(self.repo))
        report = supervisor.dry_run_report(self.project["id"])
        self.assertTrue(report["ready"])
        packet = supervisor.compiler.compile(
            self.store.get_job(report["ready"][0]["job_id"]),
            repo_facts=gitfacts.facts(self.repo)).render()
        self.assertIn("sample-repo", packet)
        self.assertIn("Policy pack" if "Policy pack" in packet else "Project policy", packet)

    def test_cli_registers_a_foreign_repo(self):
        second = self.home / "another-repo"
        shutil.copytree(FIXTURE, second)
        subprocess.run(["git", "-C", str(second), "init", "-q", "-b", "main"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(second), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(second), "-c", "user.email=t@example.com",
                        "-c", "user.name=T", "commit", "-q", "-m", "initial"],
                       check=True, capture_output=True)
        self.store.close()
        self.assertEqual(main(["init", str(second), "--name", "another"]), 0)
        from devsupervisor.state import Store
        self.store = Store.open()
        self.assertIsNotNone(self.store.get_project("another"))
