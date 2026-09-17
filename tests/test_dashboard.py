"""The dashboard observes durable state and never becomes an actor in it."""

import json
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from devsupervisor import clock, config, gates
from devsupervisor.dashboard import build_server, summary
from devsupervisor.dashboard import reader as reader_module
from devsupervisor.dashboard.reader import Reader, StateUnavailable
from devsupervisor.state import Store, leases, machine

from .support import HarnessTestCase

RUN = (machine.READY, machine.DISPATCHED, machine.RUNNING)


class DashboardTestCase(HarnessTestCase):
    """A reader pointed at the same durable state the harness just wrote."""

    def reader(self):
        reader = Reader(config.db_path())
        self.addCleanup(reader.close)
        return reader

    def running_job(self, project, goal=None, role="build", subject="widget", **fields):
        job = self.make_job(project=project, goal=goal, role=role, subject=subject, **fields)
        self.drive(job["id"], RUN)
        return self.store.get_job(job["id"])

    def finished_job(self, project, goal=None, role="build", subject="widget", **fields):
        """A job walked all the way to DONE, through the guards the harness enforces."""
        job = self.running_job(project, goal=goal, role=role, subject=subject, **fields)
        self.drive(job["id"], (machine.WORK_COMPLETE, machine.UNDER_REVIEW))
        self.store.transition(job["id"], machine.APPROVED, actor="reviewer:independent",
                              fields={"result_sha": "c" * 40})
        if role in machine.LANDABLE_ROLES:
            self.drive(job["id"], (machine.LANDING_READY, machine.LANDING, machine.VERIFIED,
                                   machine.EVALUATED), actor="landing")
        self.store.transition(job["id"], machine.DONE, actor="supervisor")
        return self.store.get_job(job["id"])


class ReadOnlyTests(DashboardTestCase):
    def test_the_connection_refuses_every_write(self):
        project = self.make_project()
        self.running_job(project)
        reader = self.reader()

        for statement in ("UPDATE jobs SET status = 'DONE'",
                          "INSERT INTO events (kind, created_at) VALUES ('x', 'y')",
                          "DELETE FROM jobs"):
            with self.assertRaises(sqlite3.OperationalError):
                reader.conn.execute(statement)

    def test_reading_state_leaves_the_database_byte_identical(self):
        project = self.make_project()
        self.running_job(project)
        self.store.close()
        before = Path(config.db_path()).read_bytes()

        reader = Reader(config.db_path())
        reader.state()
        reader.activity()
        reader.close()

        self.assertEqual(before, Path(config.db_path()).read_bytes())
        self.store = Store.open()              # the harness teardown closes it again


class StatusMappingTests(DashboardTestCase):
    def test_a_dispatched_job_is_an_active_agent(self):
        project = self.make_project()
        job = self.running_job(project, role="reviewer", subject="wire size")
        state = self.reader().state(project["id"])

        agent = _agent(state, job["id"])
        self.assertEqual(agent["status"], summary.AGENT_ACTIVE)
        self.assertEqual(agent["line"], "Reviewing wire size")
        self.assertEqual(state["status"], "RUNNING")

    def test_a_failed_job_is_not_reported_as_working(self):
        project = self.make_project()
        job = self.running_job(project)
        self.store.transition(job["id"], machine.FAILED, actor="scheduler")

        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_FAILED)
        self.assertEqual(state["status"], "BLOCKED")

    def test_a_blocked_job_is_reported_as_blocked(self):
        project = self.make_project()
        job = self.running_job(project)
        self.store.transition(job["id"], machine.BLOCKED, actor="scheduler")

        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_BLOCKED)

    def test_roles_with_no_live_job_are_shown_idle_and_carry_no_job_id(self):
        project = self.make_project()
        clock.freeze(clock.now())
        self.finished_job(project, role="evaluator", subject="old run")
        clock.advance(reader_module.RECENT_COMPLETE_SECONDS * 3)
        self.running_job(project, role="build")

        agents = self.reader().state(project["id"])["agents"]
        self.assertEqual({a["role"] for a in agents if a["status"] != summary.AGENT_IDLE},
                         {"build"}, "a job finished long ago is history, not an agent")
        idle = [a for a in agents if a["status"] == summary.AGENT_IDLE]
        self.assertTrue(idle, "a role the project uses should appear as idle capacity")
        self.assertTrue(all(agent["id"] is None for agent in idle),
                        "an idle node is role capacity, not a fabricated agent")

    def test_an_active_job_reports_how_long_it_has_been_running(self):
        project = self.make_project()
        job = self.running_job(project)
        agent = _agent(self.reader().state(project["id"]), job["id"])
        self.assertIsNotNone(agent["elapsed_s"])
        self.assertGreaterEqual(agent["elapsed_s"], 0)

    def test_the_lease_count_is_the_live_one(self):
        project = self.make_project()
        job = self.running_job(project)
        self.assertEqual(self.reader().state(project["id"])["leases"], 0)

        leases.acquire(self.store, job["id"], owner="scheduler")
        self.assertEqual(self.reader().state(project["id"])["leases"], 1)


class HumanGateTests(DashboardTestCase):
    def test_an_open_gate_takes_over_the_global_status(self):
        project = self.make_project()
        job = self.running_job(project)
        gate = gates.open_gate(self.store, "destructive", "Rewrite the index?",
                               project_id=project["id"], job_id=job["id"])

        state = self.reader().state(project["id"])
        self.assertEqual(state["status"], "WAITING FOR HUMAN")
        self.assertEqual(state["supervisor"]["status"], "WAITING FOR DECISION")
        self.assertEqual(state["supervisor"]["detail"], gate["id"])
        self.assertEqual([g["id"] for g in state["gates"]], [gate["id"]])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_WAITING)

    def test_a_decided_gate_releases_the_status(self):
        project = self.make_project()
        job = self.running_job(project)
        gate = gates.open_gate(self.store, "strategy", "Which option?",
                               project_id=project["id"], job_id=job["id"])
        gates.decide(self.store, gate["id"], approved=True, actor="human")

        state = self.reader().state(project["id"])
        self.assertNotEqual(state["status"], "WAITING FOR HUMAN")
        self.assertEqual(state["gates"], [])

    def test_the_gate_is_recorded_in_activity(self):
        project = self.make_project()
        gates.open_gate(self.store, "budget", "Approve $40 of paid runs?",
                        project_id=project["id"])

        entries = self.reader().activity(project["id"])["entries"]
        gate_rows = [e for e in entries if e["kind"] == "gate"]
        self.assertTrue(gate_rows)
        self.assertEqual(gate_rows[0]["title"], "WAITING FOR HUMAN")
        self.assertIn("Approve $40", gate_rows[0]["detail"])


class MultipleProjectTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_project("alpha")
        self.beta = self.make_project("beta")
        self.alpha_job = self.running_job(self.alpha, role="build", subject="alpha work")
        self.beta_job = self.running_job(self.beta, role="reviewer", subject="beta work")

    def test_each_project_reports_only_its_own_agents(self):
        reader = self.reader()
        alpha_ids = {a["id"] for a in reader.state("alpha")["agents"]}
        beta_ids = {a["id"] for a in reader.state("beta")["agents"]}

        self.assertIn(self.alpha_job["id"], alpha_ids)
        self.assertNotIn(self.beta_job["id"], alpha_ids)
        self.assertIn(self.beta_job["id"], beta_ids)
        self.assertNotIn(self.alpha_job["id"], beta_ids)

    def test_activity_does_not_leak_across_projects(self):
        entries = self.reader().activity("alpha")["entries"]
        self.assertTrue(entries)
        for entry in entries:
            self.assertNotEqual(entry["job_id"], self.beta_job["id"])

    def test_a_gate_in_one_project_does_not_stall_another(self):
        gates.open_gate(self.store, "strategy", "beta only", project_id=self.beta["id"])
        reader = self.reader()
        self.assertEqual(reader.state("alpha")["status"], "RUNNING")
        self.assertEqual(reader.state("beta")["status"], "WAITING FOR HUMAN")

    def test_every_project_is_listed_for_the_selector(self):
        names = {p["name"] for p in self.reader().projects()}
        self.assertEqual(names, {"alpha", "beta"})

    def test_spend_is_counted_per_project(self):
        self.assertEqual(self.reader().state("alpha")["spend"]["project_usd"], 0)


class PersistenceTests(DashboardTestCase):
    def test_history_is_identical_after_the_reader_is_thrown_away(self):
        project = self.make_project()
        job = self.running_job(project)
        self.store.transition(job["id"], machine.WORK_COMPLETE, actor="worker-1")

        first = self.reader().activity(project["id"])["entries"]
        restarted = Reader(config.db_path())
        self.addCleanup(restarted.close)
        second = restarted.activity(project["id"])["entries"]

        self.assertEqual(first, second)
        self.assertTrue(first, "durable transitions must reconstruct the log")

    def test_a_job_waiting_for_a_reviewer_is_still_visible(self):
        """WORK_COMPLETE is not an active agent, so the log must carry it."""
        project = self.make_project()
        job = self.running_job(project)
        self.store.transition(job["id"], machine.WORK_COMPLETE, actor="worker-1")

        reader = self.reader()
        working = [a for a in reader.state(project["id"])["agents"]
                   if a["status"] != summary.AGENT_IDLE]
        self.assertEqual(working, [], "a finished worker is not an active agent")
        titles = [e["title"] for e in reader.activity(project["id"])["entries"]]
        self.assertIn(f"{job['id']} work complete", titles)

    def test_no_two_activity_rows_describe_the_same_moment_twice(self):
        project = self.make_project()
        self.finished_job(project, role="evaluator", subject="an analysis")
        entries = self.reader().activity(project["id"])["entries"]
        seen = [(e["at"], e["title"]) for e in entries]
        self.assertEqual(len(seen), len(set(seen)))

    def test_a_terminal_job_stays_in_recent_activity(self):
        project = self.make_project()
        job = self.finished_job(project, subject="finished work")

        reader = self.reader()
        self.assertEqual(reader.state(project["id"])["status"], "NO ACTIVE RUN")
        titles = [e["title"] for e in reader.activity(project["id"])["entries"]]
        self.assertIn(f"{job['id']} done", titles)

    def test_the_dashboard_adds_no_table_of_its_own(self):
        """History is derived, not duplicated: no second source of truth."""
        project = self.make_project()
        self.running_job(project)
        reader = self.reader()
        reader.activity(project["id"])

        tables = {row["name"] for row in reader.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("dashboard_events", tables)
        self.assertNotIn("activity", tables)

    def test_paging_reaches_older_entries(self):
        project = self.make_project()
        for index in range(4):
            self.running_job(project, subject=f"task {index}")
        reader = self.reader()

        page = reader.activity(project["id"], limit=2)
        self.assertEqual(len(page["entries"]), 2)
        self.assertTrue(page["has_more"])
        older = reader.activity(project["id"], limit=2, before=page["entries"][-1]["at"])
        self.assertTrue(older["entries"])
        self.assertLess(older["entries"][0]["at"], page["entries"][-1]["at"])


class EmptyStateTests(DashboardTestCase):
    def test_a_project_with_no_jobs_renders_without_inventing_anything(self):
        project = self.make_project("fresh")
        state = self.reader().state(project["id"])

        self.assertEqual(state["status"], "NO ACTIVE RUN")
        self.assertEqual(state["agents"], [])
        self.assertEqual(state["pipeline"], [])
        self.assertEqual(state["gates"], [])
        self.assertEqual(state["current"], "")
        self.assertEqual(self.reader().activity(project["id"])["entries"], [])

    def test_a_runtime_root_with_no_database_fails_clearly(self):
        with self.assertRaises(StateUnavailable) as caught:
            Reader(self.home / "nothing" / "supervisor.db")
        self.assertIn("no DevSupervisor database", str(caught.exception))

    def test_an_unknown_project_is_reported_not_invented(self):
        self.make_project()
        with self.assertRaises(StateUnavailable):
            self.reader().state("does-not-exist")


class DetailTests(DashboardTestCase):
    def test_the_panel_shows_operational_metadata_only(self):
        project = self.make_project()
        job = self.running_job(project, branch="slice/x", base_sha="a" * 40,
                               model="claude-opus-5", effort="high",
                               session_id="sess-secret", metadata={"title": "Ingest rewrite"})

        detail = self.reader().detail(job["id"])
        self.assertEqual(detail["branch"], "slice/x")
        self.assertEqual(detail["base_sha"], "a" * 7)
        for forbidden in ("session_id", "transcript_path", "prompt", "output_contract",
                          "provider", "permission_mode"):
            self.assertNotIn(forbidden, detail)

    def test_a_stored_title_wins_over_the_generated_one(self):
        project = self.make_project()
        job = self.running_job(project, subject="raw slug",
                               metadata={"title": "Ingest rewrite"})
        self.assertEqual(self.reader().detail(job["id"])["action"],
                         "Implementing Ingest rewrite")


class PipelineTests(DashboardTestCase):
    def test_stages_are_derived_from_the_roles_actually_present(self):
        project = self.make_project()
        goal = self.make_goal(project=project)
        self.running_job(project, goal=goal, role="build", subject="a")
        self.running_job(project, goal=goal, role="reviewer", subject="b")

        names = [stage["name"] for stage in self.reader().state(project["id"])["pipeline"]]
        self.assertEqual(names, ["Build", "Review"])

    def test_a_blocked_job_marks_its_stage_blocked(self):
        project = self.make_project()
        goal = self.make_goal(project=project)
        job = self.running_job(project, goal=goal, role="build")
        self.store.transition(job["id"], machine.BLOCKED, actor="scheduler")

        stages = self.reader().state(project["id"])["pipeline"]
        self.assertEqual(stages[0]["state"], summary.STAGE_BLOCKED)


class SummaryTests(HarnessTestCase):
    def test_lines_are_derived_from_the_job_id_without_a_model(self):
        cases = {
            "BUILD-mcp-wire-001": ("build", "Implementing mcp wire"),
            "REVIEWER-packet-body-003": ("reviewer", "Reviewing packet body"),
            "EVALUATOR-hybrid-guarded-002": ("evaluator", "Evaluating hybrid guarded"),
        }
        for job_id, (role, expected) in cases.items():
            line = summary.action_line(
                {"id": job_id, "role": role, "status": machine.RUNNING, "metadata": {}})
            self.assertEqual(line, expected)

    def test_an_unknown_role_gets_a_neutral_verb_rather_than_a_guess(self):
        line = summary.action_line(
            {"id": "CHOREOGRAPHER-new-thing-001", "role": "choreographer",
             "status": machine.RUNNING, "metadata": {}})
        self.assertEqual(line, "Working on new thing")

    def test_lines_are_capped(self):
        job = {"id": "BUILD-" + "-".join(["verylongword"] * 9) + "-001", "role": "build",
               "status": machine.RUNNING, "metadata": {}}
        self.assertLessEqual(len(summary.action_line(job)), summary.MAX_ACTION_CHARS)


class HttpTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project("alpha")
        self.job = self.running_job(self.project)
        self.server = build_server(db_path=config.db_path(), port=0)
        self.addCleanup(self.server.reader.close)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read().decode())

    def test_the_server_binds_only_to_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_read_endpoints_answer(self):
        self.assertEqual(self.get("/api/state")["project"]["id"], "alpha")
        self.assertTrue(self.get("/api/activity?project=alpha")["entries"])
        self.assertEqual(self.get("/api/job?id=" + self.job["id"])["id"], self.job["id"])

    def test_every_mutating_method_is_refused(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            request = urllib.request.Request(self.base + "/api/state", method=method,
                                             data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 405)
            caught.exception.close()

    def test_a_malformed_query_string_does_not_error(self):
        for query in ("limit=abc", "limit=-5", "limit=99999", "before=';DROP TABLE jobs;--",
                      "kind=../../etc/passwd", "project="):
            payload = self.get("/api/activity?" + urllib.parse.quote(query, safe="=&"))
            self.assertIn("entries", payload)
        self.assertTrue(self.store.list_jobs(), "the jobs table is still there")

    def test_no_request_path_can_read_the_filesystem(self):
        for path in ("/../../etc/passwd", "/static/../reader.py", "/reader.py",
                     "/api/nope"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path, timeout=5)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()

    def test_durable_text_is_carried_as_data_not_markup(self):
        hostile = self.make_job(project=self.project, subject="x",
                                metadata={"title": "<script>alert(1)</script>"})
        self.drive(hostile["id"], RUN)
        detail = self.get("/api/job?id=" + hostile["id"])
        self.assertIn("<script>", detail["action"])       # preserved verbatim as data

        source = (Path(__file__).resolve().parents[1] / "devsupervisor" / "dashboard"
                  / "static" / "app.js").read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertFalse(sink in source,
                             f"app.js must not build markup from durable text ({sink})")


def _agent(state, job_id):
    for agent in state["agents"]:
        if agent["id"] == job_id:
            return agent
    raise AssertionError(f"{job_id} is not in the graph: {state['agents']}")
