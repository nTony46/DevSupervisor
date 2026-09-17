"""Read-only view over DevSupervisor's durable state.

The dashboard observes the supervisor; it never becomes another actor in the
system. The connection is opened `mode=ro` with `query_only` set, so a coding
mistake here fails loudly instead of writing to authoritative state.

Nothing is cached in server memory that could not be rebuilt from SQLite on the
next request, which is what makes history survive a restart for free.
"""

import json
import sqlite3
from pathlib import Path

from .. import clock, config, gitfacts
from ..errors import DevSupervisorError
from ..state import machine
from . import summary

RECENT_COMPLETE_SECONDS = 900
# How long a parked job keeps a node in the live graph once it stops moving.
STALE_AGENT_SECONDS = 86400
MAX_AGENT_NODES = 12
# The graph is a glance, not a reading surface; the full question is in `devsup gates`.
SUPERVISOR_LINE_CHARS = 120
GIT_CACHE_SECONDS = 5
DEFAULT_ACTIVITY_LIMIT = 50
MAX_ACTIVITY_LIMIT = 200

# Transitions worth a line in the activity log. The rest (PLANNED, READY,
# DISPATCHED) are bookkeeping the operator did not ask about.
#
# WORK_COMPLETE earns its line even though review usually follows immediately:
# a job whose worker has finished but whose reviewer has not started is not an
# active agent, so without this row it would be invisible on the whole page.
ACTIVITY_TRANSITIONS = {
    machine.RUNNING: ("started", "run"),
    machine.WORK_COMPLETE: ("work complete", "ok"),
    machine.UNDER_REVIEW: ("under review", "run"),
    machine.APPROVED: ("approved", "ok"),
    machine.REJECTED: ("rejected", "bad"),
    machine.REVISION_READY: ("revision started", "warn"),
    machine.LANDING: ("landing", "run"),
    machine.VERIFIED: ("verified", "ok"),
    machine.EVALUATED: ("evaluated", "ok"),
    machine.DONE: ("done", "ok"),
    machine.BLOCKED: ("blocked", "bad"),
    machine.FAILED: ("failed", "bad"),
    machine.WAITING_HUMAN: ("waiting for human", "gate"),
    machine.CANCELLED: ("cancelled", "muted"),
    machine.SUPERSEDED: ("superseded", "muted"),
    machine.PAUSED: ("paused", "muted"),
}

# Only these event kinds reach the activity log. An allowlist, so a future event
# carrying a prompt or a credential cannot leak into the UI by default.
ACTIVITY_EVENTS = {"landing.completed": ("landed", "ok")}


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

    def close(self):
        self.conn.close()

    # --- plumbing ---------------------------------------------------------

    def _rows(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params)]

    def _jobs(self, sql, params=()):
        """Job rows with the JSON columns the dashboard reads already decoded."""
        rows = self._rows(sql, params)
        for row in rows:
            row["metadata"] = _json(row.get("metadata"))
        return rows

    def _one(self, sql, params=()):
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
                    "agents": [], "pipeline": [], "gates": [], "stages_note": ""}
        project_id = project["id"]
        jobs = self._jobs("SELECT * FROM jobs WHERE project_id = ? ORDER BY updated_at DESC",
                          (project_id,))
        gates = self._open_gates(project_id)
        goal, scope_jobs = self._current_scope(jobs)
        agents = self._agents(project_id, jobs, {job["id"] for job in scope_jobs})
        lock = self._one("SELECT * FROM supervisor_lock WHERE id = 1")
        leases = self._active_leases(project_id)
        return {
            "project": {"id": project_id, "name": project["name"],
                        "repo_path": project["repo_path"]},
            "projects": self.projects(),
            "status": self._global_status(agents, gates, scope_jobs, lock),
            "supervisor": self._supervisor_node(agents, gates, goal, lock),
            "goal": goal,
            "git": self._git(project["repo_path"]),
            "spend": self._spend(project_id, scope_jobs),
            "leases": len(leases),
            "pipeline": summary.pipeline(scope_jobs),
            "current": summary.current_line(scope_jobs),
            "agents": agents,
            "gates": gates,
            "generated_at": clock.now_iso(),
        }

    CLOSED_GOALS = frozenset({"DONE", "CANCELLED", "ABANDONED"})

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
        goal_row = self._one("SELECT id, title, status FROM goals WHERE id = ?", (goal_id,))
        scope = [job for job in jobs if job["goal_id"] == goal_id]
        goal = None
        if goal_row:
            goal = {"id": goal_row["id"],
                    "title": summary.truncate(goal_row["title"], 90),
                    "status": goal_row["status"]}
        return goal, scope

    def _closed_goal_ids(self):
        return {row["id"] for row in self._rows(
            "SELECT id FROM goals WHERE status IN ('DONE', 'CANCELLED', 'ABANDONED')")}

    def _global_status(self, agents, gates, scope_jobs, lock):
        if gates:
            return "WAITING FOR HUMAN"
        if any(agent["status"] == summary.AGENT_ACTIVE for agent in agents):
            return "RUNNING"
        if lock:
            return "RUNNING" if clock.parse(lock["expires_at"]) > clock.now() else "RECOVERING"
        if any(job["status"] in (machine.BLOCKED, machine.FAILED) for job in scope_jobs):
            return "BLOCKED"
        if any(job["status"] not in machine.TERMINAL for job in scope_jobs):
            return "IDLE"
        return "NO ACTIVE RUN"

    def _supervisor_node(self, agents, gates, goal, lock):
        active = [a for a in agents if a["status"] == summary.AGENT_ACTIVE]
        if gates:
            gate = gates[-1]
            waiting = (f"{len(gates)} decisions pending" if len(gates) > 1
                       else "1 decision pending")
            return {"status": "WAITING FOR DECISION",
                    "line": summary.truncate(gate["question"], SUPERVISOR_LINE_CHARS),
                    "detail": gate["id"], "note": waiting}
        if active:
            return {"status": "RUNNING",
                    "line": f"{len(active)} agent{'s' if len(active) != 1 else ''} working",
                    "detail": goal["title"] if goal else ""}
        if lock and clock.parse(lock["expires_at"]) > clock.now():
            return {"status": "RUNNING", "line": "Planning next step",
                    "detail": goal["title"] if goal else ""}
        return {"status": "IDLE", "line": "No work dispatched",
                "detail": goal["title"] if goal else ""}

    # --- agents -----------------------------------------------------------

    def _agents(self, project_id, jobs, scope_ids=()):
        """Real jobs first; then the roles this project uses, greyed out.

        An idle node is role capacity, not a fabricated agent: it carries no job
        id, and only roles this project has actually dispatched are listed.

        A job that is running is always shown. A job merely parked or blocked is
        shown only while it is still part of the current work, because a node
        that stopped moving weeks ago is history, not an agent.
        """
        now = clock.now()
        scope_ids = set(scope_ids)
        shown, nodes = [], []
        for job in jobs:
            recent = self._recently_complete(job, now)
            status = summary.agent_status(job, recently_complete=recent)
            if status == summary.AGENT_IDLE:
                continue
            if status != summary.AGENT_ACTIVE and not self._is_current(job, scope_ids, now):
                continue
            if len(nodes) >= MAX_AGENT_NODES:
                break
            nodes.append(self._agent_node(job, status, now))
            shown.append(job["role"])
        busy = set(shown)
        for role in self._roles_used(project_id):
            if role not in busy:
                nodes.append({"id": None, "role": role, "status": summary.AGENT_IDLE,
                              "line": "", "elapsed_s": None, "reviews": None,
                              "revision_of": None})
        return nodes

    def _agent_node(self, job, status, now):
        run = self._latest_run(job["id"])
        started = self._started_at(job, run)
        elapsed = None
        if status == summary.AGENT_ACTIVE and started:
            elapsed = max(0, int((now - clock.parse(started)).total_seconds()))
        return {
            "id": job["id"],
            "role": job["role"],
            "status": status,
            "job_status": job["status"],
            "line": summary.action_line(job),
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
        return [row["role"] for row in self._rows(
            "SELECT role, COUNT(*) n FROM jobs WHERE project_id = ?"
            " GROUP BY role ORDER BY n DESC LIMIT 8", (project_id,))]

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
        now = clock.now_iso()
        return self._rows(
            "SELECT l.job_id FROM leases l JOIN jobs j ON j.id = l.job_id"
            " WHERE j.project_id = ? AND l.expires_at > ?", (project_id, now))

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
        cached = self._git_cache.get(repo_path)
        now = clock.now()
        if cached and (now - cached[0]).total_seconds() < GIT_CACHE_SECONDS:
            return cached[1]
        facts = gitfacts.facts(repo_path)
        value = {"branch": facts.get("branch"), "sha": _short(facts.get("head")),
                 "clean": facts.get("clean")} if facts.get("is_git") else {}
        self._git_cache[repo_path] = (now, value)
        return value

    # --- activity ---------------------------------------------------------

    def activity(self, project_key=None, limit=DEFAULT_ACTIVITY_LIMIT, before=None,
                 kind=None):
        """Chronological work log, reconstructed from durable state.

        Three durable sources are merged: job transitions (what moved), gate rows
        (what a human was asked), and an allowlisted set of events (what landed).
        Nothing is kept in server memory, so the log is identical after a restart.
        """
        project = self.resolve_project(project_key)
        if project is None:
            return {"entries": [], "has_more": False}
        limit = max(1, min(int(limit or DEFAULT_ACTIVITY_LIMIT), MAX_ACTIVITY_LIMIT))
        window = limit * 4
        entries = (self._transition_entries(project["id"], before, window)
                   + self._gate_entries(project["id"], before, window)
                   + self._event_entries(project["id"], before, window))
        entries = [entry for entry in entries if _matches(entry, kind)]
        entries.sort(key=lambda entry: entry["at"], reverse=True)
        return {"entries": entries[:limit], "has_more": len(entries) > limit}

    def _transition_entries(self, project_id, before, window):
        sql = ("SELECT t.job_id, t.to_status, t.reason, t.created_at, j.role, j.result_sha,"
               " j.status AS job_status, j.metadata"
               " FROM job_transitions t JOIN jobs j ON j.id = t.job_id"
               " WHERE j.project_id = ? AND t.to_status IN"
               f" ({', '.join('?' * len(ACTIVITY_TRANSITIONS))})")
        params = [project_id, *ACTIVITY_TRANSITIONS]
        if before:
            sql += " AND t.created_at < ?"
            params.append(before)
        sql += " ORDER BY t.created_at DESC LIMIT ?"
        params.append(window)
        entries = []
        for row in self._rows(sql, params):
            label, tone = ACTIVITY_TRANSITIONS[row["to_status"]]
            job = {"id": row["job_id"], "role": row["role"], "status": row["job_status"],
                   "metadata": _json(row["metadata"])}
            entries.append({
                "at": row["created_at"], "tone": tone, "kind": _tone_kind(tone),
                "title": f"{row['job_id']} {label}",
                "detail": summary.subject_of(job),
                "note": summary.truncate(row["reason"], 110),
                "job_id": row["job_id"], "role": row["role"],
            })
        return entries

    def _gate_entries(self, project_id, before, window):
        sql = "SELECT * FROM human_gates WHERE project_id = ?"
        params = [project_id]
        if before:
            sql += " AND created_at < ?"
            params.append(before)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(window)
        entries = []
        for row in self._rows(sql, params):
            entries.append({
                "at": row["created_at"], "tone": "gate", "kind": "gate",
                "title": "WAITING FOR HUMAN" if row["status"] == "OPEN" else "Human gate",
                "detail": summary.truncate(row["question"], 120),
                "note": row["id"], "job_id": row["job_id"], "role": None,
            })
            if row["decided_at"] and (not before or row["decided_at"] < before):
                entries.append({
                    "at": row["decided_at"],
                    "tone": "ok" if row["status"] == "APPROVED" else "bad",
                    "kind": "gate",
                    "title": f"Gate {row['status'].lower()}",
                    "detail": summary.truncate(row["question"], 120),
                    "note": summary.truncate(row["decision_note"], 110),
                    "job_id": row["job_id"], "role": None,
                })
        return entries

    def _event_entries(self, project_id, before, window):
        placeholders = ", ".join("?" * len(ACTIVITY_EVENTS))
        sql = (f"SELECT * FROM events WHERE project_id = ? AND kind IN ({placeholders})")
        params = [project_id, *ACTIVITY_EVENTS]
        if before:
            sql += " AND created_at < ?"
            params.append(before)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(window)
        entries = []
        for row in self._rows(sql, params):
            label, tone = ACTIVITY_EVENTS[row["kind"]]
            payload = _json(row["payload"])
            entries.append({
                "at": row["created_at"], "tone": tone, "kind": _tone_kind(tone),
                "title": label.upper(),
                "detail": _short(payload.get("sha")) or "",
                "note": summary.truncate(payload.get("lane") or "", 110),
                "job_id": row["job_id"], "role": None,
            })
        return entries


def _matches(entry, kind):
    if not kind or kind == "all":
        return True
    return entry["kind"] == kind


def _json(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _tone_kind(tone):
    return {"ok": "completed", "bad": "failed", "gate": "gate"}.get(tone, "running")


def _short(sha):
    return sha[:7] if sha else None
