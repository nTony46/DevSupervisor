"""The dashboard observes durable state and never becomes an actor in it."""

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
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

        writes = ("UPDATE jobs SET status = 'DONE'",
                  "INSERT INTO events (kind, created_at) VALUES ('x', 'y')",
                  "DELETE FROM jobs")
        for statement in writes:
            with self.assertRaises(sqlite3.OperationalError):
                reader.conn.execute(statement)

        # `query_only` is a setting; `mode=ro` is the handle. Turning the first
        # off must not be enough, or the process would hold a writable handle on
        # authoritative state one PRAGMA away from using it.
        reader.conn.execute("PRAGMA query_only=OFF")
        for statement in writes:
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

    def test_a_node_carries_what_the_card_states_about_the_job(self):
        """Branch, provider and review policy are read off the job, never invented."""
        project = self.make_project()
        job = self.running_job(project, branch="slice/rc-e", provider="claude-cli",
                               effort="high", review_policy="independent")
        agent = _agent(self.reader().state(project["id"]), job["id"])
        self.assertEqual(agent["branch"], "slice/rc-e")
        self.assertEqual(agent["provider"], "claude-cli")
        self.assertEqual(agent["effort"], "high")
        self.assertEqual(agent["review_policy"], "independent")

    def test_a_node_title_names_the_work_without_repeating_its_state(self):
        project = self.make_project()
        job = self.running_job(project, subject="refresh auth tokens")
        agent = _agent(self.reader().state(project["id"]), job["id"])
        self.assertEqual(agent["title"], "refresh auth tokens")
        self.assertNotEqual(agent["title"], agent["line"])

    def test_a_node_without_a_branch_says_so(self):
        project = self.make_project()
        job = self.running_job(project)
        agent = _agent(self.reader().state(project["id"]), job["id"])
        self.assertIsNone(agent["branch"])

    def test_agent_library_separates_a_role_from_its_live_instances(self):
        project = self.make_project()
        self.running_job(project, role="builder", provider="codex", model="gpt-6-sol",
                         effort="medium")
        profile = next(item for item in self.reader().state(project["id"])["agent_library"]
                       if item["role"] == "build")
        self.assertEqual(profile["instances"], 1)
        self.assertEqual(profile["provider"], "codex")
        self.assertEqual(profile["model"], "gpt-6-sol")
        self.assertEqual(profile["effort"], "medium")
        self.assertEqual(profile["access"], "isolated writer")

    def test_unused_agent_roles_report_policy_defaults(self):
        project = self.make_project()
        profile = next(item for item in self.reader().state(project["id"])["agent_library"]
                       if item["role"] == "reviewer")
        self.assertEqual(profile["instances"], 0)
        self.assertEqual(profile["source"], "policy default")
        self.assertEqual(profile["effort"], "high")

    def test_the_workflow_header_carries_its_saved_description(self):
        project = self.make_project()
        goal = self.make_goal(project=project, description="Coordinate five isolated lanes.")
        self.running_job(project, goal=goal)
        self.assertEqual(self.reader().state(project["id"])["goal"]["description"],
                         "Coordinate five isolated lanes.")

    def test_the_lease_count_is_the_live_one(self):
        project = self.make_project()
        job = self.running_job(project)
        self.assertEqual(self.reader().state(project["id"])["leases"], 0)

        leases.acquire(self.store, job["id"], owner="scheduler")
        self.assertEqual(self.reader().state(project["id"])["leases"], 1)


class LivenessTests(DashboardTestCase):
    """A status column is a claim about the past; the graph is a claim about now."""

    def stale_running_job(self, project, age_seconds, **fields):
        job = self.running_job(project, **fields)
        old = clock.iso(clock.now() - timedelta(seconds=age_seconds))
        self.store.conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?",
                                (old, job["id"]))
        return self.store.get_job(job["id"])

    def test_a_running_job_nobody_holds_is_not_a_live_agent(self):
        project = self.make_project()
        job = self.stale_running_job(
            project, reader_module.ACTIVE_WITHOUT_LEASE_SECONDS * 2)

        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_STALE)
        self.assertEqual(state["active_count"], 0)
        self.assertNotEqual(state["status"], "RUNNING")

    def test_a_stale_agent_does_not_report_a_running_clock(self):
        project = self.make_project()
        job = self.stale_running_job(project, 86400 * 900)
        self.assertIsNone(_agent(self.reader().state(project["id"]), job["id"])["elapsed_s"])

    def test_the_current_line_does_not_describe_a_stalled_job_as_working(self):
        project = self.make_project()
        self.stale_running_job(project, 86400 * 900, role="reviewer", subject="wire size")
        state = self.reader().state(project["id"])
        self.assertNotIn("Reviewing", state["current"])
        self.assertIn("Stalled", state["current"])

    def test_a_stale_agent_does_not_describe_itself_as_working(self):
        project = self.make_project()
        job = self.stale_running_job(project, 86400 * 900, subject="schema drift")
        line = _agent(self.reader().state(project["id"]), job["id"])["line"]
        self.assertIn("Stalled", line)
        self.assertNotIn("Implementing", line)

    def test_a_held_lease_keeps_an_old_job_live(self):
        """The lease is the evidence; without it the status column is just a claim."""
        project = self.make_project()
        job = self.stale_running_job(
            project, reader_module.ACTIVE_WITHOUT_LEASE_SECONDS * 2)
        leases.acquire(self.store, job["id"], owner="scheduler")

        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_ACTIVE)
        self.assertEqual(state["active_count"], 1)

    def test_an_expired_lease_does_not_keep_a_job_live(self):
        project = self.make_project()
        job = self.stale_running_job(
            project, reader_module.ACTIVE_WITHOUT_LEASE_SECONDS * 2)
        leases.acquire(self.store, job["id"], owner="scheduler", ttl_seconds=1)
        self.store.conn.execute(
            "UPDATE leases SET expires_at = ? WHERE job_id = ?",
            (clock.iso(clock.now() - timedelta(seconds=60)), job["id"]))

        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_STALE)

    def test_a_job_that_declared_a_long_runtime_is_not_called_stalled(self):
        """The scheduler honours metadata.timeout_s and never renews a lease."""
        project = self.make_project()
        job = self.stale_running_job(project, 7000, role="evaluator",
                                     metadata={"timeout_s": 10800})

        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_ACTIVE)
        self.assertEqual(state["active_count"], 1)

    def test_a_declared_runtime_cannot_be_unbounded(self):
        """metadata reaches a job from a worker's own subtask request.

        `delegation._create` copies that dict wholesale, so `timeout_s` is
        model-authored. An unbounded value would let a dead job hold a live node
        for ever, which is the defect this grace period exists to prevent.
        """
        project = self.make_project()
        hostile = ("inf", "Infinity", "1e9", 1e300, 10 ** 30, float("inf"),
                   "99999999999999999999999999")
        for index, declared in enumerate(hostile):
            job = self.stale_running_job(project, 86400 * 30, subject=f"forever {index}",
                                         metadata={"timeout_s": declared})
            self.assertEqual(
                _agent(self.reader().state(project["id"]), job["id"])["status"],
                summary.AGENT_STALE, f"timeout_s={declared!r} defeated the liveness check")

    def test_a_nonsense_runtime_is_garbage_not_the_maximum(self):
        """`inf` is not a declaration of the longest allowed run; it is nonsense.

        Aged between the default grace and the ceiling, so a value read as
        garbage goes stale while a real 24h declaration does not.
        """
        project = self.make_project()
        age = (reader_module.ACTIVE_WITHOUT_LEASE_SECONDS
               + reader_module.MAX_DECLARED_GRACE_SECONDS) // 2
        nonsense = self.stale_running_job(project, age, subject="nonsense",
                                          metadata={"timeout_s": "inf"})
        declared = self.stale_running_job(
            project, age, subject="declared", role="evaluator",
            metadata={"timeout_s": reader_module.MAX_DECLARED_GRACE_SECONDS})

        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, nonsense["id"])["status"], summary.AGENT_STALE)
        self.assertEqual(_agent(state, declared["id"])["status"], summary.AGENT_ACTIVE)

    def test_an_enormous_integer_does_not_take_the_page_down(self):
        """float(10**400) raises OverflowError, which is not a ValueError."""
        project = self.make_project()
        job = self.stale_running_job(project, 86400 * 30, subject="huge",
                                     metadata={"timeout_s": 10 ** 400})
        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_STALE)

    def test_a_garbage_runtime_falls_back_to_the_default(self):
        project = self.make_project()
        for index, declared in enumerate(("abc", None, -5, True, {"a": 1}, [1, 2], "nan")):
            job = self.stale_running_job(project, 7000, subject=f"junk {index}",
                                         metadata={"timeout_s": declared})
            self.assertEqual(
                _agent(self.reader().state(project["id"]), job["id"])["status"],
                summary.AGENT_STALE, f"timeout_s={declared!r} was treated as a real grace")

    def test_a_declared_runtime_does_not_excuse_a_job_forever(self):
        project = self.make_project()
        job = self.stale_running_job(project, 86400 * 400, role="evaluator",
                                     metadata={"timeout_s": 10800})
        self.assertEqual(
            _agent(self.reader().state(project["id"]), job["id"])["status"],
            summary.AGENT_STALE)

    def test_a_freshly_running_job_needs_no_lease_to_count(self):
        project = self.make_project()
        job = self.running_job(project)
        state = self.reader().state(project["id"])
        self.assertEqual(_agent(state, job["id"])["status"], summary.AGENT_ACTIVE)
        self.assertEqual(state["active_count"], 1)

    def test_a_parked_job_outside_the_current_work_leaves_the_graph(self):
        """Blocked months ago under a goal nobody is on any more is history.

        The age is absolute on purpose: scaling the fixture by the constant
        under test would make the constant impossible to get wrong.
        """
        project = self.make_project()
        old_goal = self.make_goal(project=project, title="last quarter")
        stranded = self.running_job(project, goal=old_goal, subject="abandoned")
        self.store.transition(stranded["id"], machine.BLOCKED, actor="scheduler")
        self.store.conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ?",
            (clock.iso(clock.now() - timedelta(days=400)), stranded["id"]))
        current = self.make_goal(project=project, title="this week")
        live = self.running_job(project, goal=current, subject="today")

        ids = {a["id"] for a in self.reader().state(project["id"])["agents"]}
        self.assertIn(live["id"], ids)
        self.assertNotIn(stranded["id"], ids)

    def test_a_parked_job_in_the_current_work_stays_visible(self):
        project = self.make_project()
        goal = self.make_goal(project=project)
        job = self.running_job(project, goal=goal)
        self.store.transition(job["id"], machine.BLOCKED, actor="scheduler")

        ids = {a["id"] for a in self.reader().state(project["id"])["agents"]}
        self.assertIn(job["id"], ids)


class NodeCapTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.roles = ("build", "reviewer", "researcher", "planner", "security", "qa",
                      "evaluator", "landing")
        # Enough live jobs to spill past the cap, however high the cap is set.
        self.per_role = reader_module.MAX_AGENT_NODES // len(self.roles) + 1
        for role in self.roles:
            for index in range(self.per_role):
                self.running_job(self.project, role=role, subject=f"{role} {index}")
        self.state = self.reader().state(self.project["id"])

    def test_the_active_count_is_the_real_one_not_the_displayed_one(self):
        self.assertEqual(self.state["active_count"], len(self.roles) * self.per_role)
        self.assertLessEqual(len(self.state["agents"]), reader_module.MAX_AGENT_NODES)

    def test_hidden_agents_are_declared_rather_than_dropped_silently(self):
        self.assertEqual(self.state["hidden_agents"],
                         len(self.roles) * self.per_role - reader_module.MAX_AGENT_NODES)

    def test_no_role_with_running_work_is_offered_as_idle_capacity(self):
        busy = {job["role"] for job in
                self.store.list_jobs(project_id=self.project["id"], status=machine.RUNNING)}
        for agent in self.state["agents"]:
            if agent["status"] == summary.AGENT_IDLE:
                self.assertNotIn(agent["role"], busy,
                                 f"{agent['role']} has running jobs but is shown idle")

    def test_idle_capacity_the_cap_dropped_is_declared_too(self):
        """Live jobs one short of the cap leave one slot; the roles that miss out are counted."""
        project = self.make_project("crowded")
        for index in range(reader_module.MAX_AGENT_NODES - 1):
            self.running_job(project, role="build", subject=f"shard {index}")
        for role in ("reviewer", "qa", "security", "planner"):
            old = self.finished_job(project, role=role, subject=f"{role} history")
            self.store.conn.execute(          # long finished: capacity, not a node
                "UPDATE jobs SET updated_at = ? WHERE id = ?",
                (clock.iso(clock.now() - timedelta(days=400)), old["id"]))

        state = self.reader().state(project["id"])
        drawn_idle = [a for a in state["agents"] if a["status"] == summary.AGENT_IDLE]
        self.assertEqual(len(state["agents"]), reader_module.MAX_AGENT_NODES)
        self.assertEqual(state["hidden_agents"], 4 - len(drawn_idle))
        self.assertGreater(state["hidden_agents"], 0)

    def test_idle_capacity_is_counted_beyond_the_roles_that_fit(self):
        """A mature project uses more roles than the graph can draw.

        The role query is unlimited on purpose: a role truncated there would be
        a node dropped without ever being counted as hidden.
        """
        project = self.make_project("mature")
        roles = tuple(f"role{index:02d}" for index in range(reader_module.MAX_AGENT_NODES + 3))
        for role in roles:
            job = self.make_job(project=project, role=role, subject=f"{role} history")
            self.store.conn.execute(
                "UPDATE jobs SET status = 'DONE', updated_at = ? WHERE id = ?",
                (clock.iso(clock.now() - timedelta(days=400)), job["id"]))

        state = self.reader().state(project["id"])
        drawn = [a for a in state["agents"] if a["status"] == summary.AGENT_IDLE]
        self.assertEqual(len(drawn), reader_module.MAX_AGENT_NODES)
        self.assertEqual(state["hidden_agents"], len(roles) - reader_module.MAX_AGENT_NODES)
        self.assertGreater(state["hidden_agents"], 0)

    def test_the_cap_bounds_the_whole_graph(self):
        self.assertLessEqual(len(self.state["agents"]), reader_module.MAX_AGENT_NODES)


class ActivityFilterTests(DashboardTestCase):
    """The filter that answers "what failed" must not be the one that hides it."""

    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.failed = self.running_job(self.project, subject="the old failure")
        self.store.transition(self.failed["id"], machine.FAILED, actor="scheduler")
        old = clock.iso(clock.now() - timedelta(days=30))
        self.store.conn.execute(
            "UPDATE job_transitions SET created_at = ? WHERE job_id = ?",
            (old, self.failed["id"]))
        for index in range(60):                 # bury it under newer noise
            self.running_job(self.project, subject=f"newer {index}")

    def test_an_old_failure_is_found_under_a_page_of_newer_activity(self):
        entries = self.reader().activity(self.project["id"], limit=50, kind="failed")["entries"]
        self.assertTrue(entries, "a failure in durable state must be reachable")
        self.assertEqual(entries[0]["job_id"], self.failed["id"])

    def test_the_unfiltered_log_still_pages(self):
        page = self.reader().activity(self.project["id"], limit=50)
        self.assertEqual(len(page["entries"]), 50)
        self.assertTrue(page["has_more"])

    def test_each_filter_returns_only_its_own_kind(self):
        reader = self.reader()
        for kind in ("completed", "failed", "gate", "running"):
            for entry in reader.activity(self.project["id"], limit=50, kind=kind)["entries"]:
                self.assertEqual(entry["kind"], kind)

    def test_a_recent_decision_on_an_old_gate_is_reachable(self):
        """The decision sorts by when it was answered, not when it was asked."""
        gate = gates.open_gate(self.store, "strategy", "asked long ago",
                               project_id=self.project["id"])
        self.store.conn.execute("UPDATE human_gates SET created_at = ? WHERE id = ?",
                                (clock.iso(clock.now() - timedelta(days=400)), gate["id"]))
        # More noise than any per-source window, so the row can only be found
        # by sorting on the column the decision actually carries.
        noise = reader_module.MAX_ACTIVITY_LIMIT * 2 + reader_module.TIE_WINDOW_SLACK
        for index in range(noise):          # bury the question, not the answer
            other = gates.open_gate(self.store, "budget", f"noise {index}",
                                    project_id=self.project["id"])
            gates.decide(self.store, other["id"], approved=True, actor="human")
        gates.decide(self.store, gate["id"], approved=True, actor="human")

        reader = self.reader()
        for kind in ("all", "completed"):
            entries = reader.activity(self.project["id"], limit=50, kind=kind)["entries"]
            notes = [e["detail"] for e in entries if e["title"] == "Gate approved"]
            self.assertIn("asked long ago", notes,
                          f"the newest decision is unreachable under kind={kind}")

    def test_a_rejected_gate_answers_the_failed_filter(self):
        gate = gates.open_gate(self.store, "strategy", "a refused plan",
                               project_id=self.project["id"])
        gates.decide(self.store, gate["id"], approved=False, actor="human")
        entries = self.reader().activity(self.project["id"], limit=50, kind="failed")["entries"]
        self.assertIn("Gate rejected", [e["title"] for e in entries])

    def test_a_decision_recorded_under_an_unknown_status_still_appears(self):
        """Selecting only today's two statuses would hide tomorrow's silently."""
        gate = gates.open_gate(self.store, "strategy", "an expired question",
                               project_id=self.project["id"])
        self.store.conn.execute(
            "UPDATE human_gates SET status = 'EXPIRED', decided_at = ? WHERE id = ?",
            (clock.now_iso(), gate["id"]))

        reader = self.reader()
        for kind in ("all", "other"):
            titles = [e["title"] for e in
                      reader.activity(self.project["id"], limit=50, kind=kind)["entries"]]
            self.assertIn("Gate expired", titles, f"unreachable under kind={kind}")
        for kind in ("completed", "failed", "gate"):
            titles = [e["title"] for e in
                      reader.activity(self.project["id"], limit=50, kind=kind)["entries"]]
            self.assertNotIn("Gate expired", titles)

    def test_a_gate_is_reachable_through_the_gate_filter_alone(self):
        gates.open_gate(self.store, "strategy", "an old question",
                        project_id=self.project["id"])
        entries = self.reader().activity(self.project["id"], limit=50, kind="gate")["entries"]
        self.assertTrue(any(e["title"] == "WAITING FOR HUMAN" for e in entries))


class PagingInvariantTests(DashboardTestCase):
    """Walking the log page by page must see exactly what one big read sees."""

    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        for index in range(20):
            self.running_job(self.project, subject=f"task {index}")
        for index in range(4):
            gate = gates.open_gate(self.store, "budget", f"question {index}",
                                   project_id=self.project["id"])
            gates.decide(self.store, gate["id"], approved=index % 2 == 0, actor="human")
        # A pile of entries sharing one instant: the case a `<` cursor skips.
        self.store.conn.execute(
            "UPDATE job_transitions SET created_at = '2026-05-01T11:59:11+00:00'"
            " WHERE id IN (SELECT id FROM job_transitions WHERE to_status = 'RUNNING'"
            "              LIMIT 6)")
        self.store.conn.execute(
            "UPDATE human_gates SET decided_at = '2026-05-01T11:59:11+00:00'")

    def walk(self, kind, limit):
        reader = self.reader()
        seen, before = [], None
        for _ in range(200):
            page = reader.activity(self.project["id"], limit=limit, before=before, kind=kind)
            if not page["entries"]:
                break
            seen.extend(page["entries"])
            if not page["has_more"]:
                break
            before = page["entries"][-1]["at"]
        return seen

    def test_paging_loses_nothing_and_repeats_nothing(self):
        reader = self.reader()
        for kind in ("all", "completed", "failed", "gate"):
            truth = reader.activity(self.project["id"], limit=200, kind=kind)["entries"]
            for limit in (1, 2, 3, 5, 7, 13):
                walked = self.walk(kind, limit)
                self.assertEqual(sorted(map(_key, walked)), sorted(map(_key, truth)),
                                 f"kind={kind} limit={limit} paged differently")

    def test_a_group_too_large_to_page_is_not_reported_as_the_end(self):
        """A timestamp cursor cannot walk a group with no order inside it.

        It must say so rather than hide the remainder behind a finished log.
        """
        instant = "2026-05-01T11:59:11+00:00"
        self.store.conn.execute("UPDATE job_transitions SET created_at = ?", (instant,))
        self.store.conn.execute(
            "UPDATE human_gates SET created_at = ?, decided_at = ?", (instant, instant))
        page = self.reader().activity(self.project["id"], limit=5)
        self.assertTrue(page["has_more"], "the rest of the group was reported as absent")
        self.assertTrue(page.get("truncated_group"))

    def test_a_complete_log_is_never_declared_truncated(self):
        """Distinct timestamps at limit=1 must not look like a tied group."""
        page = self.reader().activity(self.project["id"], limit=1)
        self.assertFalse(page.get("truncated_group"))

    def test_a_page_never_ends_inside_a_group_sharing_one_timestamp(self):
        reader = self.reader()
        page = reader.activity(self.project["id"], limit=5)
        if page["has_more"]:
            boundary = page["entries"][-1]["at"]
            remainder = reader.activity(self.project["id"], limit=200, before=boundary)
            self.assertFalse([e for e in remainder["entries"] if e["at"] == boundary],
                             "entries at the cursor timestamp were left unreachable")


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

    def test_a_slow_repository_does_not_block_unrelated_endpoints(self):
        """git runs outside the connection lock, so one slow repo is not an outage.

        Blocked on an Event rather than a sleep: the assertion is that the second
        request completes *while* the first is still inside gitfacts, which is a
        fact about locking, not about timing.
        """
        from devsupervisor import gitfacts

        entered, release = threading.Event(), threading.Event()
        original = gitfacts.facts

        def blocking_facts(repo):
            entered.set()
            release.wait(timeout=10)
            return original(repo)

        gitfacts.facts = blocking_facts
        self.addCleanup(setattr, gitfacts, "facts", original)
        self.server.reader._git_cache.clear()

        slow = threading.Thread(target=lambda: self.get("/api/state"), daemon=True)
        slow.start()
        self.assertTrue(entered.wait(timeout=5), "the state request never reached git")
        try:
            self.assertIn("projects", self.get("/api/projects"))
        finally:
            release.set()
            slow.join(timeout=10)

    def test_a_slow_repository_is_looked_up_once_not_once_per_request(self):
        """Concurrent polls reuse the last answer instead of each forking git."""
        from devsupervisor import gitfacts

        calls, entered, release = [], threading.Event(), threading.Event()
        original = gitfacts.facts

        def counting_facts(repo):
            calls.append(repo)
            entered.set()
            release.wait(timeout=10)
            return original(repo)

        gitfacts.facts = counting_facts
        self.addCleanup(setattr, gitfacts, "facts", original)
        reader = self.server.reader
        reader._git_cache.clear()
        reader._git_cache[self.project["repo_path"]] = (clock.now() - timedelta(hours=1), {})

        threads = [threading.Thread(target=lambda: self.get("/api/state"), daemon=True)
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(timeout=5))
        time.sleep(0.3)                      # let the rest arrive and find it in flight
        self.assertEqual(len(calls), 1, f"one refresh should serve them all, saw {len(calls)}")
        release.set()
        for thread in threads:
            thread.join(timeout=10)

    def test_a_cold_cache_is_also_looked_up_once(self):
        """With nothing to fall back on, waiters wait rather than each forking git."""
        from devsupervisor import gitfacts

        calls, entered, release = [], threading.Event(), threading.Event()
        original = gitfacts.facts

        def counting_facts(repo):
            calls.append(repo)
            entered.set()
            release.wait(timeout=10)
            return original(repo)

        gitfacts.facts = counting_facts
        self.addCleanup(setattr, gitfacts, "facts", original)
        self.server.reader._git_cache.clear()          # nothing cached at all

        threads = [threading.Thread(target=lambda: self.get("/api/state"), daemon=True)
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(timeout=5))
        time.sleep(0.3)
        self.assertEqual(len(calls), 1,
                         f"a cold cache should still look up once, saw {len(calls)}")
        release.set()
        for thread in threads:
            thread.join(timeout=15)

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
        """Probes that WOULD resolve to a real file if routing touched the disk.

        `/../reader.py` sits next to the static directory and `../../../../etc`
        climbs out of the package: a filesystem-backed route serves both. Only
        paths that exist can tell an explicit route map from a lucky 404.
        """
        for path in ("/../reader.py", "/../summary.py", "/../../cli.py",
                     "/../../../../../../etc/passwd", "/static/index.html",
                     "/api/nope"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path, timeout=5)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()

    def test_only_the_published_assets_are_served(self):
        """A file that appears in the static directory is not thereby public."""
        static = (Path(__file__).resolve().parents[1] / "devsupervisor" / "dashboard"
                  / "static")
        planted = static / "not-published.txt"
        planted.write_text("should never be served")
        self.addCleanup(planted.unlink)

        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/not-published.txt", timeout=5)
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


STATIC = Path(__file__).resolve().parents[1] / "devsupervisor" / "dashboard" / "static"


class ConsoleStylingTests(DashboardTestCase):
    """The stylesheet is a contract with the reader's vocabulary: every state the
    reader can emit must have a visual, or a state would render as nothing."""

    def setUp(self):
        super().setUp()
        self.css = (STATIC / "style.css").read_text()
        self.js = (STATIC / "app.js").read_text()

    def test_the_review_handoff_is_never_a_fixed_string(self):
        """The footer under a builder reports the ledger: the reviewer that exists,
        or the job's own status. A constant would claim a review nobody dispatched."""
        self.assertNotIn("awaiting candidate", self.js)
        self.assertIn("handoffFor(", self.js)
        for word in ("reviewers.get(agent.id)", "REVIEWED.has(agent.job_status)",
                     "agent.review_policy === 'none'"):
            self.assertIn(word, self.js)

    def test_roles_are_matched_regardless_of_case(self):
        self.assertIn("toLowerCase()", self.js[self.js.index("const isBuilder"):][:200])

    def test_the_workflow_shell_exposes_library_lanes_and_assignments(self):
        html = (STATIC / "index.html").read_text()
        for label in ("Agent library", "Agent assignments", "By lane"):
            self.assertIn(label, html)
        self.assertIn('id="agent-library-page"', html)
        for renderer in ("renderLanes(", "renderAgentLibrary(", "showLibrary(",
                         "renderProviderFilter("):
            self.assertIn(renderer, self.js)
        self.assertIn('.app-main[data-view="library"] .agent-library-page', self.css)

    def test_every_agent_status_has_a_node_style(self):
        for status in (summary.AGENT_ACTIVE, summary.AGENT_WAITING, summary.AGENT_BLOCKED,
                       summary.AGENT_COMPLETE, summary.AGENT_FAILED, summary.AGENT_IDLE,
                       summary.AGENT_STALE):
            self.assertIn(f'.node[data-status="{status}"]', self.css, status)

    def test_every_coloured_global_status_has_a_pill_style(self):
        """IDLE and NO ACTIVE RUN fall through to the dormant default on purpose."""
        for status in ("RUNNING", "WAITING FOR HUMAN", "BLOCKED", "RECOVERING"):
            self.assertIn(f'.status[data-state="{status}"]', self.css, status)

    def test_every_activity_tone_has_a_row_style(self):
        tones = {tone for _, tone, _ in reader_module.ACTIVITY_TRANSITIONS.values()}
        tones |= {tone for _, tone, _ in reader_module.ACTIVITY_EVENTS.values()}
        tones.discard("muted")  # the neutral row is the default style
        for tone in tones:
            self.assertIn(f'.activity li[data-tone="{tone}"]', self.css, tone)
            self.assertIn(f"{tone}:", self.js.split("const MARKS")[1].split(";")[0], tone)

    def test_a_human_decision_is_the_amber_state_everywhere(self):
        """One colour for one meaning: the pill, the supervisor and the row."""
        import re
        for selector in (r'\.status\[data-state="WAITING FOR HUMAN"\]',
                         r'\.node\.supervisor\.gate', r'\.activity li\[data-tone="gate"\]'):
            # Every rule whose selector list ends with this selector; one of
            # them must set the amber tone.
            bodies = re.findall(selector + r"\s*\{([^}]*)\}", self.css)
            self.assertTrue(bodies, selector)
            self.assertTrue(any("--hold" in body for body in bodies), selector)
        self.assertIn("HUMAN DECISION REQUIRED", self.js)

    def test_reduced_motion_removes_every_animation(self):
        block = self.css.split("@media (prefers-reduced-motion: reduce)")[1]
        self.assertIn("animation: none !important", block)

    def test_the_page_references_only_published_files(self):
        html = (STATIC / "index.html").read_text()
        for name in ("/style.css", "/app.js"):
            self.assertIn(name, html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("@import", self.css)
        self.assertNotIn("url(", self.css)


def _key(entry):
    return (entry["at"], entry["title"], entry["detail"], entry["kind"])


def _agent(state, job_id):
    for agent in state["agents"]:
        if agent["id"] == job_id:
            return agent
    raise AssertionError(f"{job_id} is not in the graph: {state['agents']}")
