"""The CLI and the loop read the same durable state, so they cannot disagree."""

import io
import subprocess
from contextlib import redirect_stdout

from devsupervisor import gates
from devsupervisor.cli import EXIT_ERROR, EXIT_OK, main
from devsupervisor.state import Store, machine
from tests.support import HarnessTestCase


class CliTestCase(HarnessTestCase):
    """CLI commands open their own store, so ours must be closed around calls."""

    def cli(self, argv, expect=EXIT_OK):
        self.store.close()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        self.store = Store.open()
        self.assertEqual(code, expect, buffer.getvalue())
        return buffer.getvalue()

    def make_repo(self, name="repo"):
        path = self.home / name
        path.mkdir()
        (path / "README.md").write_text("# demo\n")
        for argv in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.email=t@e.com", "-c", "user.name=T",
                      "commit", "-q", "-m", "init"]):
            subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True)
        return path


class CommandTests(CliTestCase):
    def test_init_goal_plan_status_and_jobs(self):
        repo = self.make_repo()
        self.assertIn("initialised project", self.cli(["init", str(repo), "--name", "demo"]))
        output = self.cli(["goal", "add", "demo", "Add workspace support"])
        goal_id = output.split()[0]
        plan_output = self.cli(["plan", goal_id])
        self.assertIn("workflow=feature", plan_output)
        self.assertIn("BUILD-add-workspace-support-001", plan_output)
        self.assertIn("Add workspace support", self.cli(["status"]))
        self.assertIn("BUILD-add-workspace-support-001", self.cli(["jobs", "--project", "demo"]))
        detail = self.cli(["job", "show", "BUILD-add-workspace-support-001"])
        self.assertIn("review      independent", detail)

    def test_dry_run_reports_without_dispatching_anything(self):
        repo = self.make_repo()
        self.cli(["init", str(repo), "--name", "demo"])
        goal_id = self.cli(["goal", "add", "demo", "Add workspace support"]).split()[0]
        self.cli(["plan", goal_id])
        before = [(j["id"], j["status"]) for j in self.store.list_jobs("demo")]

        output = self.cli(["run", "demo", "--dry-run"])
        self.assertIn("INVESTIGATOR-add-workspace-support-001", output)
        self.assertIn("packet", output)

        after = [(j["id"], j["status"]) for j in self.store.list_jobs("demo")]
        self.assertEqual(before, after)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM runs")
                         .fetchone()["n"], 0)

    def test_doctor_reports_and_exits_zero_on_a_healthy_install(self):
        output = self.cli(["doctor"])
        self.assertIn("runtime root", output)
        self.assertIn("default provider", output)

    def test_gates_can_be_listed_and_approved(self):
        repo = self.make_repo()
        self.cli(["init", str(repo), "--name", "demo"])
        job = self.store.create_job("demo", "feature", "build", "widget")
        gate = gates.open_gate(self.store, "budget", "Spend $40?", project_id="demo",
                               job_id=job["id"])
        self.assertIn(gate["id"], self.cli(["gates"]))
        self.assertIn("APPROVED", self.cli(["approve", gate["id"], "--actor", "tony"]))
        self.assertEqual(self.store.get_job(job["id"])["status"], machine.READY)

    def test_pause_parks_ready_work(self):
        repo = self.make_repo()
        self.cli(["init", str(repo), "--name", "demo"])
        goal_id = self.cli(["goal", "add", "demo", "Add workspace support"]).split()[0]
        self.cli(["plan", goal_id])
        self.cli(["pause", "--project", "demo", "--reason", "stepping away"])
        statuses = {job["status"] for job in self.store.list_jobs("demo")}
        self.assertEqual(statuses, {machine.PAUSED})

    def test_memory_curate_and_retrospect_run(self):
        repo = self.make_repo()
        self.cli(["init", str(repo), "--name", "demo"])
        from devsupervisor.memory import MemoryStore
        MemoryStore("demo").write("decisions", "Land by SHA", "always the reviewed sha")
        output = self.cli(["memory", "curate", "demo"])
        self.assertIn("authoritative memory is unchanged", output)
        self.assertIn("Nothing here has been applied", self.cli(["retrospect", "demo"]))
        self.assertIn("Land by SHA", self.cli(["memory", "list", "demo"]))

    def test_import_handoffs_reports_logical_jobs(self):
        directory = self.home / "handoffs"
        directory.mkdir()
        (directory / "one.md").write_text(
            "# Agent / Role\n- **Agent number:** Agent 4\n\n# Current Status\n**COMPLETE**\n\n"
            "| Branch | `feature/x` |\n| HEAD | `abc1234` |\n")
        output = self.cli(["import-handoffs", str(directory)])
        self.assertIn("1 document(s)", output)
        self.assertIn("branch:feature/x", output)


class ClaudeMdTests(CliTestCase):
    def test_writes_the_file_when_there_is_none(self):
        repo = self.make_repo()
        self.assertIn("wrote", self.cli(["claude-md", str(repo)]))
        text = (repo / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("# DevSupervisor"))
        self.assertIn("devsup gates", text)

    def test_appends_to_an_existing_file_and_keeps_what_was_there(self):
        repo = self.make_repo()
        (repo / "CLAUDE.md").write_text("# My project\n\nRun `make test` before committing.\n")
        self.assertIn("appended", self.cli(["claude-md", str(repo)]))
        text = (repo / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("# My project\n"))
        self.assertIn("Run `make test`", text)
        self.assertIn("\n\n# DevSupervisor", text)

    def test_running_it_twice_changes_nothing(self):
        repo = self.make_repo()
        self.cli(["claude-md", str(repo)])
        before = (repo / "CLAUDE.md").read_text()
        self.assertIn("nothing changed", self.cli(["claude-md", str(repo)]))
        self.assertEqual((repo / "CLAUDE.md").read_text(), before)

    def test_refuses_a_missing_directory(self):
        self.cli(["claude-md", str(self.home / "nope")], expect=EXIT_ERROR)


class FailureModeTests(CliTestCase):
    def test_init_refuses_a_missing_path(self):
        self.cli(["init", str(self.home / "nope")], expect=EXIT_ERROR)

    def test_init_refuses_a_non_git_directory_by_default(self):
        plain = self.home / "plain"
        plain.mkdir()
        self.cli(["init", str(plain)], expect=EXIT_ERROR)

    def test_init_accepts_a_non_git_directory_when_told_to(self):
        plain = self.home / "plain2"
        plain.mkdir()
        output = self.cli(["init", str(plain), "--name", "plain2", "--allow-non-git"])
        self.assertIn("initialised project", output)

    def test_duplicate_project_is_refused(self):
        repo = self.make_repo()
        self.cli(["init", str(repo), "--name", "demo"])
        self.cli(["init", str(repo), "--name", "demo"], expect=EXIT_ERROR)

    def test_unknown_policy_pack_is_refused_at_init(self):
        repo = self.make_repo("repo2")
        self.cli(["init", str(repo), "--name", "demo2", "--pack", "nope"],
                 expect=EXIT_ERROR)

    def test_running_with_a_paid_provider_needs_the_flag(self):
        repo = self.make_repo()
        self.cli(["init", str(repo), "--name", "demo"])
        self.cli(["run", "demo", "--provider", "claude-cli"], expect=EXIT_ERROR)
