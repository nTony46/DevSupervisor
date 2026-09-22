"""Read-only view over DevSupervisor's durable state.

The dashboard observes the supervisor; it never becomes another actor in the
system. The connection is opened `mode=ro` with `query_only` set, so a coding
mistake here fails loudly instead of writing to authoritative state.

Nothing is cached in server memory that could not be rebuilt from SQLite on the
next request, which is what makes history survive a restart for free.
"""

import json
import math
import sqlite3
import threading
from pathlib import Path

from .. import clock, config, gitfacts
from ..errors import DevSupervisorError
from ..policy import permissions, routing, tools as tool_policy
from ..state import machine
from . import summary

RECENT_COMPLETE_SECONDS = 900
# How long a parked job keeps a node in the live graph once it stops moving.
STALE_AGENT_SECONDS = 86400
# How long a job may claim to be running with nobody holding its lease before
# the graph stops believing it. `leases.recover_orphans` calls exactly this
# state "nobody's work until it is put back", but it only runs when the
# scheduler runs -- which is precisely when nobody is watching the dashboard.
#
# A job that declares a longer runtime gets it: the scheduler honours
# `metadata.timeout_s` when it runs a job, and leases are never renewed, so a
# genuine long run outlives its own lease. Believing the shorter of the two
# would report live work as stalled.
ACTIVE_WITHOUT_LEASE_SECONDS = 3600
# A ceiling on that extension. `metadata.timeout_s` reaches a job from a worker's
# own subtask request, which `delegation._create` copies wholesale, so it is
# model-authored input. Without a bound, a job could declare itself alive
# forever and the graph would show a dead agent with a running clock -- the
# defect this grace period exists to avoid, reintroduced from the other side.
MAX_DECLARED_GRACE_SECONDS = 86400
# A safety bound, not a page size: every live job and every role a project has
# ever dispatched is drawn, and only a runaway (hundreds of live jobs) is cut.
# A mature project uses fifteen-odd roles, so this is far above real use.
MAX_AGENT_NODES = 48
# The graph is a glance, not a reading surface; the full question is in `devsup gates`.
SUPERVISOR_LINE_CHARS = 120
GIT_CACHE_SECONDS = 5
# How long a caller with nothing cached waits for the refresh already running.
GIT_WAIT_SECONDS = 30
DEFAULT_ACTIVITY_LIMIT = 50
MAX_ACTIVITY_LIMIT = 200
TIE_WINDOW_SLACK = 16

# Transitions worth a line in the activity log. The rest (PLANNED, READY,
# DISPATCHED) are bookkeeping the operator did not ask about.
#
# WORK_COMPLETE earns its line even though review usually follows immediately:
# a job whose worker has finished but whose reviewer has not started is not an
# active agent, so without this row it would be invisible on the whole page.
# label, tone, filter kind. The kind is what the UI's filters select on, and it
# is applied in SQL: filtering a fixed window in Python would let a single old
# failure fall off the end of a page full of newer noise and report "nothing".
ACTIVITY_TRANSITIONS = {
    machine.RUNNING: ("started", "run", "running"),
    machine.WORK_COMPLETE: ("work complete", "ok", "completed"),
    machine.UNDER_REVIEW: ("under review", "run", "running"),
    machine.APPROVED: ("approved", "ok", "completed"),
    machine.REJECTED: ("rejected", "bad", "failed"),
    machine.REVISION_READY: ("revision started", "warn", "running"),
    machine.LANDING: ("landing", "run", "running"),
    machine.VERIFIED: ("verified", "ok", "completed"),
    machine.EVALUATED: ("evaluated", "ok", "completed"),
    machine.DONE: ("done", "ok", "completed"),
    machine.BLOCKED: ("blocked", "bad", "failed"),
    machine.FAILED: ("failed", "bad", "failed"),
    machine.WAITING_HUMAN: ("waiting for human", "gate", "gate"),
    machine.CANCELLED: ("cancelled", "muted", "other"),
    machine.SUPERSEDED: ("superseded", "muted", "other"),
    machine.PAUSED: ("paused", "muted", "other"),
}

# Only these event kinds reach the activity log. An allowlist, so a future event
# carrying a prompt or a credential cannot leak into the UI by default.
ACTIVITY_EVENTS = {"landing.completed": ("landed", "ok", "completed")}

ALL_KINDS = "all"
# A goal in one of these is finished; its jobs are history, not current work.
CLOSED_GOAL_STATUSES = ("DONE", "CANCELLED", "ABANDONED")
# A decided gate reads as a completion or a failure, selected in SQL.
DECIDED_GATE_KINDS = {"APPROVED": "completed", "REJECTED": "failed"}


class StateUnavailable(DevSupervisorError):
    """The durable state could not be opened for reading."""


class Reader:
    """Everything the dashboard knows, read straight from durable state."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else config.db_path()
        if not self.db_path.exists():
            raise StateUnavailable(
                f"no DevSupervisor database at {self.db_path}; run `devsup init` first")
        try:
            self.conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only=ON")
            self.conn.execute("SELECT 1 FROM projects LIMIT 1")
        except sqlite3.Error as exc:
            raise StateUnavailable(f"cannot read {self.db_path}: {exc}") from exc
        self._git_cache = {}
        self._git_inflight = {}
        # Guards the connection only. Git runs outside it: six subprocesses with
        # a 30s timeout each must never block an unrelated endpoint.
        self._lock = threading.RLock()

    def close(self):
        self.conn.close()

    # --- plumbing ---------------------------------------------------------

    def _rows(self, sql, params=()):
        with self._lock:
            return [dict(row) for row in self.conn.execute(sql, params)]

    def _jobs(self, sql, params=()):
        """Job rows with the JSON columns the dashboard reads already decoded."""
        rows = self._rows(sql, params)
        for row in rows:
            row["metadata"] = _json(row.get("metadata"))
        return rows

    def _one(self, sql, params=()):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # --- projects ---------------------------------------------------------

    def projects(self):
        return [{"id": row["id"], "name": row["name"]}
                for row in self._rows("SELECT id, name FROM projects ORDER BY name")]

    def resolve_project(self, key=None):
        """A project by id or name; otherwise the one with the newest activity."""
        if key:
            found = self._one("SELECT * FROM projects WHERE id = ? OR name = ?", (key, key))
            if not found:
                raise StateUnavailable(f"no such project: {key!r}")
            return found
        newest = self._one(
            "SELECT p.* FROM projects p JOIN jobs j ON j.project_id = p.id"
            " ORDER BY j.updated_at DESC LIMIT 1")
        return newest or self._one("SELECT * FROM projects ORDER BY created_at LIMIT 1")

    # --- state ------------------------------------------------------------

    def state(self, project_key=None):
        project = self.resolve_project(project_key)
        if project is None:
            return {"project": None, "status": "NO ACTIVE RUN", "projects": [],
                    "agents": [], "pipeline": [], "gates": []}
        project_id = project["id"]
        jobs = self._jobs("SELECT * FROM jobs WHERE project_id = ? ORDER BY updated_at DESC",
                          (project_id,))
        gates = self._open_gates(project_id)
        goal, scope_jobs = self._current_scope(jobs)
        leases = self._active_leases(project_id)
        agents, counts = self._agents(project_id, jobs, {job["id"] for job in scope_jobs},
                                      live_leases=leases)
        lock = self._one("SELECT * FROM supervisor_lock WHERE id = 1")
        return {
            "project": {"id": project_id, "name": project["name"],
                        "repo_path": project["repo_path"]},
            "projects": self.projects(),
            "status": self._global_status(counts, gates, scope_jobs, lock),
            "supervisor": self._supervisor_node(counts, gates, goal, lock),
            "goal": goal,
            "git": self._git(project["repo_path"]),
            "spend": self._spend(project_id, scope_jobs),
            "leases": len(leases),
            "pipeline": summary.pipeline(scope_jobs),
            "current": summary.current_line(agents),
            "agents": agents,
            "agent_library": self._agent_library(jobs, agents),
            "active_count": counts["active"],
            "hidden_agents": counts["hidden"],
            "gates": gates,
            "generated_at": clock.now_iso(),
        }

    def _current_scope(self, jobs):
        """The goal the supervisor is working on now, and the jobs under it.

        Jobs are ordered newest-first. A job left BLOCKED months ago is not
        "current" just because nothing newer is unfinished, so an anchor on a
        closed goal loses to the most recently touched work.
        """
        closed = self._closed_goal_ids()
        live = [job for job in jobs if job["status"] not in machine.TERMINAL]
        anchor = next((job for job in live if job["status"] in machine.ACTIVE), None)
        anchor = anchor or next(
            (job for job in live if job["goal_id"] not in closed), None)
        anchor = anchor or (jobs[0] if jobs else None)
        if anchor is None:
            return None, []
        goal_id = anchor["goal_id"]
        if not goal_id:
            return None, [anchor]
        goal_row = self._one(
            "SELECT id, title, description, status FROM goals WHERE id = ?", (goal_id,))
        scope = [job for job in jobs if job["goal_id"] == goal_id]
        goal = None
        if goal_row:
            goal = {"id": goal_row["id"],
                    "title": summary.truncate(goal_row["title"], 90),
                    "description": summary.truncate(goal_row.get("description"), 180),
                    "status": goal_row["status"]}
        return goal, scope

    def _closed_goal_ids(self):
        placeholders = ", ".join("?" * len(CLOSED_GOAL_STATUSES))
        return {row["id"] for row in self._rows(
            f"SELECT id FROM goals WHERE status IN ({placeholders})",
            tuple(CLOSED_GOAL_STATUSES))}

    def _global_status(self, counts, gates, scope_jobs, lock):
        if gates:
            return "WAITING FOR HUMAN"
        if counts["active"]:
            return "RUNNING"
        if lock:
            return "RUNNING" if clock.parse(lock["expires_at"]) > clock.now() else "RECOVERING"
        if any(job["status"] in (machine.BLOCKED, machine.FAILED) for job in scope_jobs):
            return "BLOCKED"
        if any(job["status"] not in machine.TERMINAL for job in scope_jobs):
            return "IDLE"
        return "NO ACTIVE RUN"

    def _supervisor_node(self, counts, gates, goal, lock):
        active = counts["active"]
        if gates:
            gate = gates[-1]
            waiting = (f"{len(gates)} decisions pending" if len(gates) > 1
                       else "1 decision pending")
            return {"status": "WAITING FOR DECISION",
                    "line": summary.truncate(gate["question"], SUPERVISOR_LINE_CHARS),
                    "detail": gate["id"], "note": waiting}
        if active:
            return {"status": "RUNNING",
                    "line": f"{active} agent{'s' if active != 1 else ''} working",
                    "detail": goal["title"] if goal else ""}
        if lock and clock.parse(lock["expires_at"]) > clock.now():
            return {"status": "RUNNING", "line": "Planning next step",
                    "detail": goal["title"] if goal else ""}
        return {"status": "IDLE", "line": "No work dispatched",
                "detail": goal["title"] if goal else ""}

    # --- agents -----------------------------------------------------------

    def _agents(self, project_id, jobs, scope_ids=(), live_leases=()):
        """Real jobs first; then the roles this project uses, greyed out.

        An idle node is role capacity, not a fabricated agent: it carries no job
        id, and only roles this project has actually dispatched are listed.

        A job that is running is shown while it is genuinely running. A job
        merely parked or blocked is shown only while it is still part of the
        current work, because a node that stopped moving weeks ago is history.
        """
        now = clock.now()
        scope_ids = set(scope_ids)
        leased = set(live_leases)
        nodes, busy = [], set()
        for job in jobs:
            status = self._status_for(job, now, leased)
            if status == summary.AGENT_IDLE:
                continue
            if status not in _ALWAYS_SHOWN and not self._is_current(job, scope_ids, now):
                continue
            # Every role with live work is busy, including one whose nodes fall
            # past the display cap: otherwise the graph would offer it as free
            # capacity while three of its jobs are running.
            busy.add(job["role"])
            nodes.append(self._agent_node(job, status, now))
        nodes.sort(key=_node_order)
        shown = nodes[:MAX_AGENT_NODES]
        hidden_jobs = len(nodes) - len(shown)

        idle_roles = [role for role in self._roles_used(project_id) if role not in busy]
        room = MAX_AGENT_NODES - len(shown)
        for role in idle_roles[:room]:
            shown.append({"id": None, "role": role, "status": summary.AGENT_IDLE,
                          "line": "", "elapsed_s": None, "reviews": None,
                          "revision_of": None})
        hidden_roles = len(idle_roles) - min(room, len(idle_roles))

        return shown, {
            "active": sum(1 for n in nodes if n["status"] == summary.AGENT_ACTIVE),
            # Both kinds of omission are declared: a job node the cap dropped,
            # and a role whose idle capacity there was no room left to draw.
            "hidden": hidden_jobs + hidden_roles,
        }

    def _status_for(self, job, now, leased):
        """The graph's status for a job, with orphans told apart from workers.

        DevSupervisor's own reconciliation treats DISPATCHED or RUNNING with no
        lease holder as orphaned work. A dashboard that repeats the status
        column would show a crashed worker as a live agent with a running clock.
        """
        status = summary.agent_status(job, recently_complete=self._recently_complete(job, now))
        if status != summary.AGENT_ACTIVE:
            return status
        if job["id"] in leased:
            return status
        age = (now - clock.parse(job["updated_at"])).total_seconds()
        return status if age <= self._grace_for(job) else summary.AGENT_STALE

    @staticmethod
    def _grace_for(job):
        """How long this job may run unleased before the graph disbelieves it.

        The declared value is untrusted: bounded, finite, and never negative.
        """
        declared = (job.get("metadata") or {}).get("timeout_s")
        try:
            declared = float(declared)
        except (TypeError, ValueError, OverflowError):
            declared = 0                 # a 400-digit integer is not a timeout
        if not math.isfinite(declared):
            declared = 0                 # `inf` declares nothing, so it is not the ceiling
        # The floor absorbs the rest: booleans, zero and negatives all land on it.
        return max(ACTIVE_WITHOUT_LEASE_SECONDS,
                   min(declared, MAX_DECLARED_GRACE_SECONDS))

    def _agent_node(self, job, status, now):
        run = self._latest_run(job["id"])
        started = self._started_at(job, run)
        elapsed = None
        if status == summary.AGENT_ACTIVE and started:
            elapsed = max(0, int((now - clock.parse(started)).total_seconds()))
        line = (summary.stalled_line(job) if status == summary.AGENT_STALE
                else summary.action_line(job))
        return {
            "id": job["id"],
            "role": job["role"],
            "status": status,
            "job_status": job["status"],
            "line": line,
            # The card already names the state directly above this line. Keep
            # its headline about the work itself, so a stalled card does not
            # waste both visible lines repeating "Stalled, no lease holder".
            "title": summary.truncate(summary.subject_of(job), 80),
            "provider": (run or {}).get("provider") or job["provider"],
            "model": (run or {}).get("model_resolved") or job["model"],
            "effort": (run or {}).get("effort") or job["effort"],
            "branch": job["branch"],
            "review_policy": job["review_policy"],
            "elapsed_s": elapsed,
            "reviews": job["reviews_job_id"],
            "revision_of": job["revision_of"],
        }

    def _is_current(self, job, scope_ids, now):
        if job["id"] in scope_ids:
            return True
        age = (now - clock.parse(job["updated_at"])).total_seconds()
        return age <= STALE_AGENT_SECONDS

    def _recently_complete(self, job, now):
        if job["status"] not in machine.TERMINAL:
            return False
        age = (now - clock.parse(job["updated_at"])).total_seconds()
        return age <= RECENT_COMPLETE_SECONDS

    def _roles_used(self, project_id):
        """Every role this project has dispatched, busiest first.

        Unlimited on purpose: the display cap decides what is drawn, and a role
        truncated here would be a node dropped without being counted.
        """
        return [row["role"] for row in self._rows(
            "SELECT role, COUNT(*) n FROM jobs WHERE project_id = ?"
            " GROUP BY role ORDER BY n DESC, role", (project_id,))]

    @staticmethod
    def _agent_library(jobs, agents):
        """Reusable roles and their effective recent assignment settings.

        This is descriptive state for the observer. It deliberately does not
        invent editable profiles or resolve provider discovery on every poll.
        A role with no routed job reports the built-in policy tier and effort;
        a role that has run reports the durable values recorded on its newest
        job. The UI can therefore distinguish a reusable role from each live
        instance without becoming a second configuration store.
        """
        newest = {}
        for job in jobs:  # jobs arrive newest first
            role = "build" if str(job["role"]).lower() == "builder" else job["role"]
            newest.setdefault(role, job)
        live = {}
        for agent in agents:
            if agent.get("id"):
                role = "build" if str(agent["role"]).lower() == "builder" else agent["role"]
                live[role] = live.get(role, 0) + 1
        default = routing.DEFAULT_POLICY["default"]
        configured = routing.DEFAULT_POLICY.get("roles") or {}
        profiles = []
        for role in routing.ROUTED_ROLES:
            job = newest.get(role)
            policy = {**default, **configured.get(role, {})}
            if role in permissions.PRIVILEGED_ROLES:
                access = "shared state"
            elif tool_policy.is_read_only(role):
                access = "read only"
            else:
                access = "isolated writer"
            profiles.append({
                "role": role,
                "provider": job.get("provider") if job else None,
                "model": (job.get("model") if job else None) or policy.get("tier"),
                "effort": (job.get("effort") if job else None) or policy.get("effort"),
                "access": access,
                "instances": live.get(role, 0),
                "source": "recent assignment" if job else "policy default",
            })
        return profiles

    def _latest_run(self, job_id):
        return self._one(
            "SELECT * FROM runs WHERE job_id = ? ORDER BY started_at DESC LIMIT 1", (job_id,))

    def _started_at(self, job, run):
        if run and run["started_at"]:
            return run["started_at"]
        row = self._one(
            "SELECT created_at FROM job_transitions WHERE job_id = ? AND to_status = ?"
            " ORDER BY id DESC LIMIT 1", (job["id"], machine.RUNNING))
        return row["created_at"] if row else None

    def detail(self, job_id):
        """The read-only side panel. An explicit field list, never `SELECT *`."""
        job = next(iter(self._jobs("SELECT * FROM jobs WHERE id = ?", (job_id,))), None)
        if not job:
            raise StateUnavailable(f"no such job: {job_id!r}")
        run = self._latest_run(job_id)
        cost = self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) total FROM runs WHERE job_id = ?", (job_id,))
        fields = {
            "id": job["id"], "role": job["role"], "state": job["status"],
            "summary": summary.truncate(job["scope"], 240),
            "action": summary.action_line(job),
            "job_type": job["job_type"], "risk": job["risk"],
            "branch": job["branch"], "base_sha": _short(job["base_sha"]),
            "result_sha": _short(job["result_sha"]), "worktree": job["worktree"],
            "model": (run or {}).get("model_resolved") or job["model"],
            "effort": (run or {}).get("effort") or job["effort"],
            "attempt": job["attempt"], "revision": job["revision_count"],
            "revision_of": job["revision_of"], "reviews": job["reviews_job_id"],
            "started_at": self._started_at(job, run),
            "cost_usd": round(cost["total"], 4) if cost and cost["total"] else None,
            "artifacts": [
                {"kind": row["kind"], "summary": summary.truncate(row["summary"], 160)}
                for row in self._rows(
                    "SELECT kind, summary FROM artifacts WHERE job_id = ?"
                    " ORDER BY created_at DESC LIMIT 5", (job_id,))],
        }
        return {key: value for key, value in fields.items() if value not in (None, "", [])}

    # --- facts ------------------------------------------------------------

    def _open_gates(self, project_id):
        return [{"id": row["id"], "kind": row["kind"],
                 "question": summary.truncate(row["question"], 160),
                 "job_id": row["job_id"], "created_at": row["created_at"]}
                for row in self._rows(
                    "SELECT * FROM human_gates WHERE status = 'OPEN' AND project_id = ?"
                    " ORDER BY created_at", (project_id,))]

    def _active_leases(self, project_id):
        """Job ids whose lease is held and unexpired."""
        now = clock.now_iso()
        return [row["job_id"] for row in self._rows(
            "SELECT l.job_id FROM leases l JOIN jobs j ON j.id = l.job_id"
            " WHERE j.project_id = ? AND l.expires_at > ?", (project_id, now))]

    def _spend(self, project_id, scope_jobs):
        total = self._one(
            "SELECT COALESCE(SUM(r.cost_usd), 0) total FROM runs r"
            " JOIN jobs j ON j.id = r.job_id WHERE j.project_id = ?", (project_id,))
        scope_total = 0.0
        ids = [job["id"] for job in scope_jobs]
        if ids:
            placeholders = ", ".join("?" * len(ids))
            scope_total = self._one(
                f"SELECT COALESCE(SUM(cost_usd), 0) total FROM runs"
                f" WHERE job_id IN ({placeholders})", ids)["total"]
        return {"project_usd": round(total["total"], 2), "scope_usd": round(scope_total, 2)}

    def _git(self, repo_path):
        """Branch and SHA for a repository, cached and looked up once at a time.

        Git is slow and can hang, so it runs outside the connection lock. Only
        one caller refreshes a given repository: others reuse the previous
        answer if there is one, and wait for the refresh if there is not.
        """
        now = clock.now()
        with self._lock:
            cached = self._git_cache.get(repo_path)
            if cached and (now - cached[0]).total_seconds() < GIT_CACHE_SECONDS:
                return cached[1]
            running = self._git_inflight.get(repo_path)
            if running is not None and cached:
                return cached[1]          # a stale answer now beats a fresh one later
            if running is None:
                running = threading.Event()
                self._git_inflight[repo_path] = running
                mine = True
            else:
                mine = False
        if not mine:
            running.wait(timeout=GIT_WAIT_SECONDS)
            with self._lock:
                cached = self._git_cache.get(repo_path)
            return cached[1] if cached else {}
        try:
            facts = gitfacts.facts(repo_path)      # deliberately outside the lock
            value = {"branch": facts.get("branch"), "sha": _short(facts.get("head")),
                     "clean": facts.get("clean")} if facts.get("is_git") else {}
            with self._lock:
                # Stamped on completion: a slow call must not be born already stale.
                self._git_cache[repo_path] = (clock.now(), value)
            return value
        finally:
            with self._lock:
                self._git_inflight.pop(repo_path, None)
            running.set()                 # only after the result is readable

    # --- activity ---------------------------------------------------------

    def activity(self, project_key=None, limit=DEFAULT_ACTIVITY_LIMIT, before=None,
                 kind=None):
        """Chronological work log, reconstructed from durable state.

        Three durable sources are merged: job transitions (what moved), gate rows
        (what a human was asked), and an allowlisted set of events (what landed).
        Nothing is kept in server memory, so the log is identical after a restart.

        The kind filter is pushed into each query rather than applied to the
        result. Filtering afterwards would search only the newest rows, so one
        old failure under a thousand newer transitions would report "nothing".
        """
        project = self.resolve_project(project_key)
        if project is None:
            return {"entries": [], "has_more": False}
        limit = _clamp_limit(limit)
        kind = kind or ALL_KINDS
        # Wider than the page so a group of entries sharing one timestamp can be
        # seen whole and the cursor can step past it cleanly.
        window = limit * 2 + TIE_WINDOW_SLACK
        entries = (self._transition_entries(project["id"], before, window, kind)
                   + self._gate_entries(project["id"], before, window, kind)
                   + self._event_entries(project["id"], before, window, kind))
        entries.sort(key=lambda entry: entry["at"], reverse=True)
        page = _page_on_a_clean_boundary(entries, limit)
        # A group of entries sharing one instant that is larger than the window
        # cannot be paged by a timestamp cursor: there is no order within it to
        # resume from. Saying so beats ending the log as though nothing remains.
        # A single-entry page trivially shares its own timestamp, so requiring
        # more than one keeps `limit=1` from declaring a group on every page.
        saturated = len(entries) >= window and len(page) > 1 and all(
            entry["at"] == page[-1]["at"] for entry in page)
        return {"entries": page,
                "has_more": len(entries) > len(page) or saturated,
                "truncated_group": saturated}

    def _transition_entries(self, project_id, before, window, kind):
        wanted = [status for status, row in ACTIVITY_TRANSITIONS.items()
                  if kind == ALL_KINDS or row[2] == kind]
        if not wanted:
            return []
        sql = ("SELECT t.job_id, t.to_status, t.reason, t.created_at, j.role,"
               " j.status AS job_status, j.metadata"
               " FROM job_transitions t JOIN jobs j ON j.id = t.job_id"
               f" WHERE j.project_id = ? AND t.to_status IN ({', '.join('?' * len(wanted))})")
        params = [project_id, *wanted]
        if before:
            sql += " AND t.created_at < ?"
            params.append(before)
        sql += " ORDER BY t.created_at DESC LIMIT ?"
        params.append(window)
        entries = []
        for row in self._rows(sql, params):
            label, tone, entry_kind = ACTIVITY_TRANSITIONS[row["to_status"]]
            job = {"id": row["job_id"], "role": row["role"], "status": row["job_status"],
                   "metadata": _json(row["metadata"])}
            entries.append({
                "at": row["created_at"], "tone": tone, "kind": entry_kind,
                "title": f"{row['job_id']} {label}",
                "detail": summary.subject_of(job),
                "note": summary.truncate(row["reason"], 110),
                "job_id": row["job_id"], "role": row["role"],
            })
        return entries

    def _gate_entries(self, project_id, before, window, kind):
        """Gates raised and gates answered, as two queries.

        A decision is a separate moment from the question. Deriving both from
        one window ordered by `created_at` made the decision on a long-open gate
        unreachable: it sorts by when it was answered, but it could only be
        found by when it was asked.
        """
        return (self._gate_opened_entries(project_id, before, window, kind)
                + self._gate_decided_entries(project_id, before, window, kind))

    def _gate_opened_entries(self, project_id, before, window, kind):
        if kind not in (ALL_KINDS, "gate"):
            return []
        sql = "SELECT * FROM human_gates WHERE project_id = ?"
        params = [project_id]
        if before:
            sql += " AND created_at < ?"
            params.append(before)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(window)
        return [{
            "at": row["created_at"], "tone": "gate", "kind": "gate",
            "title": "WAITING FOR HUMAN" if row["status"] == "OPEN" else "Human gate",
            "detail": summary.truncate(row["question"], 120), "note": row["id"],
            "job_id": row["job_id"], "role": None,
        } for row in self._rows(sql, params)]

    def _gate_decided_entries(self, project_id, before, window, kind):
        """Any gate carrying a decision, whatever the decision was called.

        Selecting only the two statuses known today would silently hide a
        decision recorded under a status added tomorrow, so an unrecognised one
        is surfaced under "other" rather than dropped.
        """
        known = tuple(DECIDED_GATE_KINDS)
        sql = "SELECT * FROM human_gates WHERE project_id = ? AND decided_at IS NOT NULL"
        params = [project_id]
        if kind == ALL_KINDS:
            pass
        elif kind in DECIDED_GATE_KINDS.values():
            wanted = [status for status, mapped in DECIDED_GATE_KINDS.items()
                      if mapped == kind]
            sql += f" AND status IN ({', '.join('?' * len(wanted))})"
            params.extend(wanted)
        elif kind == "other":
            sql += f" AND status NOT IN ({', '.join('?' * len(known))})"
            params.extend(known)
        else:
            return []
        if before:
            sql += " AND decided_at < ?"
            params.append(before)
        sql += " ORDER BY decided_at DESC LIMIT ?"
        params.append(window)
        return [{
            "at": row["decided_at"],
            "tone": {"APPROVED": "ok", "REJECTED": "bad"}.get(row["status"], "muted"),
            "kind": DECIDED_GATE_KINDS.get(row["status"], "other"),
            "title": f"Gate {row['status'].lower()}",
            "detail": summary.truncate(row["question"], 120),
            "note": summary.truncate(row["decision_note"], 110),
            "job_id": row["job_id"], "role": None,
        } for row in self._rows(sql, params)]

    def _event_entries(self, project_id, before, window, kind):
        wanted = [name for name, row in ACTIVITY_EVENTS.items()
                  if kind == ALL_KINDS or row[2] == kind]
        if not wanted:
            return []
        sql = ("SELECT * FROM events WHERE project_id = ?"
               f" AND kind IN ({', '.join('?' * len(wanted))})")
        params = [project_id, *wanted]
        if before:
            sql += " AND created_at < ?"
            params.append(before)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(window)
        entries = []
        for row in self._rows(sql, params):
            label, tone, entry_kind = ACTIVITY_EVENTS[row["kind"]]
            payload = _json(row["payload"])
            entries.append({
                "at": row["created_at"], "tone": tone, "kind": entry_kind,
                "title": label.upper(),
                "detail": _short(payload.get("sha")) or "",
                "note": summary.truncate(payload.get("lane") or "", 110),
                "job_id": row["job_id"], "role": None,
            })
        return entries


# Statuses that earn a node however old the job is: they are claims about now.
_ALWAYS_SHOWN = frozenset({summary.AGENT_ACTIVE, summary.AGENT_STALE})
# Live work first, then things needing attention, then the rest.
_NODE_RANK = {summary.AGENT_ACTIVE: 0, summary.AGENT_WAITING: 1, summary.AGENT_STALE: 2,
              summary.AGENT_FAILED: 3, summary.AGENT_BLOCKED: 4, summary.AGENT_COMPLETE: 5}


def _node_order(node):
    return (_NODE_RANK.get(node["status"], 9), node["id"] or "")


def _page_on_a_clean_boundary(entries, limit):
    """End a page between timestamps, never inside a group sharing one.

    The next page is fetched with `... < the last timestamp returned`, so a page
    ending in the middle of several entries stamped at the same instant would
    skip the rest of that instant for ever. Retreating to the previous timestamp
    costs a few rows and loses none. When the whole page is one instant there is
    nothing to retreat to, so the entire group is served instead and the page
    runs slightly long -- the one case where the cursor cannot otherwise move
    without dropping something.
    """
    page = entries[:limit]
    if len(entries) <= limit or not page:
        return page
    boundary = page[-1]["at"]
    if entries[limit]["at"] != boundary:
        return page              # the group ends inside the page; the cursor is clean
    trimmed = [entry for entry in page if entry["at"] != boundary]
    if trimmed:
        return trimmed
    return [entry for entry in entries if entry["at"] == boundary]


def _clamp_limit(value):
    """A query string is user input; a bad one gets the default, not a traceback."""
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = DEFAULT_ACTIVITY_LIMIT
    return max(1, min(requested, MAX_ACTIVITY_LIMIT))


def _json(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _short(sha):
    return sha[:7] if sha else None
