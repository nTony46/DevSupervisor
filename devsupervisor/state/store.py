"""The durable store: projects, goals, plans, jobs, dependencies, transitions.

Every mutation that matters goes through here, inside a transaction, and writes
an audit row. Nothing in this module asks a model anything.
"""

import json
import sqlite3

from .. import clock, ids
from ..errors import NotFound, TransitionGuardFailed
from . import db, machine

# Columns stored as JSON text but handed to callers as Python values.
_JSON_COLUMNS = {
    "projects": ("metadata",),
    "goals": ("acceptance_criteria",),
    "jobs": ("acceptance_criteria", "blockers", "metadata"),
    "events": ("payload",),
    "runs": ("result",),
    "artifacts": ("metadata",),
    "policies": ("body", "evidence"),
}

_JOB_WRITABLE = frozenset(
    """priority risk repo worktree base_sha branch scope non_goals
    acceptance_criteria output_contract review_policy attempt max_attempts
    revision_count max_revisions revision_of reviews_job_id lands_job_id
    provider model session_id session_policy result_sha blockers metadata
    plan_id goal_id""".split()
)


def decode(table, row):
    """sqlite3.Row -> plain dict with JSON columns parsed."""
    if row is None:
        return None
    out = dict(row)
    for column in _JSON_COLUMNS.get(table, ()):
        if column in out and isinstance(out[column], str):
            out[column] = json.loads(out[column])
    return out


def _encode(table, values):
    out = dict(values)
    for column in _JSON_COLUMNS.get(table, ()):
        if column in out and not isinstance(out[column], str):
            out[column] = json.dumps(out[column])
    return out


class Store:
    """Thin, explicit persistence layer over SQLite."""

    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def open(cls, path=None):
        return cls(db.connect(path))

    def close(self):
        self.conn.close()

    # --- projects ---------------------------------------------------------

    def create_project(self, name, repo_path, policy_pack=None, vcs="git", metadata=None):
        row = _encode(
            "projects",
            {
                "id": ids.slug(name, max_words=6),
                "name": name,
                "repo_path": str(repo_path),
                "policy_pack": policy_pack,
                "vcs": vcs,
                "metadata": metadata or {},
                "created_at": clock.now_iso(),
            },
        )
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id, name, repo_path, policy_pack, vcs, metadata, created_at)"
                " VALUES (:id, :name, :repo_path, :policy_pack, :vcs, :metadata, :created_at)",
                row,
            )
        return self.get_project(row["id"])

    def get_project(self, key):
        row = db.one(
            self.conn, "SELECT * FROM projects WHERE id = ? OR name = ?", (key, key)
        )
        return decode("projects", row)

    def require_project(self, key):
        project = self.get_project(key)
        if project is None:
            raise NotFound(f"no such project: {key!r}")
        return project

    def list_projects(self):
        return [decode("projects", r) for r in db.all_rows(self.conn, "SELECT * FROM projects ORDER BY name")]

    # --- goals ------------------------------------------------------------

    def create_goal(self, project_id, title, description="", acceptance_criteria=None,
                    workflow=None, risk="MEDIUM"):
        now = clock.now_iso()
        row = _encode(
            "goals",
            {
                "id": ids.new_id("goal"),
                "project_id": project_id,
                "title": title,
                "description": description,
                "acceptance_criteria": list(acceptance_criteria or []),
                "workflow": workflow,
                "risk": risk,
                "status": "OPEN",
                "created_at": now,
                "updated_at": now,
            },
        )
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO goals (id, project_id, title, description, acceptance_criteria,"
                " workflow, risk, status, created_at, updated_at) VALUES (:id, :project_id,"
                " :title, :description, :acceptance_criteria, :workflow, :risk, :status,"
                " :created_at, :updated_at)",
                row,
            )
        return self.get_goal(row["id"])

    def get_goal(self, goal_id):
        return decode("goals", db.one(self.conn, "SELECT * FROM goals WHERE id = ?", (goal_id,)))

    def list_goals(self, project_id=None, status=None):
        sql = "SELECT * FROM goals WHERE 1=1"
        params = []
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        return [decode("goals", r) for r in db.all_rows(self.conn, sql + " ORDER BY created_at", params)]

    def set_goal_status(self, goal_id, status):
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE goals SET status = ?, updated_at = ? WHERE id = ?",
                (status, clock.now_iso(), goal_id),
            )
        return self.get_goal(goal_id)

    # --- plans ------------------------------------------------------------

    def create_plan(self, goal_id, workflow, rationale=""):
        """A new plan supersedes the previous active one. Replanning is normal."""
        with db.transaction(self.conn):
            prior = db.one(
                self.conn,
                "SELECT MAX(version) AS v FROM plans WHERE goal_id = ?",
                (goal_id,),
            )["v"]
            version = (prior or 0) + 1
            self.conn.execute(
                "UPDATE plans SET status = 'SUPERSEDED' WHERE goal_id = ? AND status = 'ACTIVE'",
                (goal_id,),
            )
            plan_id = ids.new_id("plan")
            self.conn.execute(
                "INSERT INTO plans (id, goal_id, version, workflow, status, rationale, created_at)"
                " VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
                (plan_id, goal_id, version, workflow, rationale, clock.now_iso()),
            )
        return decode("plans", db.one(self.conn, "SELECT * FROM plans WHERE id = ?", (plan_id,)))

    def active_plan(self, goal_id):
        return decode(
            "plans",
            db.one(
                self.conn,
                "SELECT * FROM plans WHERE goal_id = ? AND status = 'ACTIVE'"
                " ORDER BY version DESC LIMIT 1",
                (goal_id,),
            ),
        )

    # --- jobs -------------------------------------------------------------

    def next_sequence(self, project_id, role_prefix, subject):
        prefix = f"{role_prefix.upper()}-{ids.slug(subject)}-"
        rows = db.all_rows(
            self.conn,
            "SELECT id FROM jobs WHERE project_id = ? AND id LIKE ?",
            (project_id, prefix + "%"),
        )
        return len(rows) + 1

    def create_job(self, project_id, job_type, role, subject, goal_id=None, plan_id=None,
                   status=machine.PLANNED, depends_on=(), job_id=None, **fields):
        unknown = set(fields) - _JOB_WRITABLE
        if unknown:
            raise ValueError(f"unknown job fields: {sorted(unknown)}")
        now = clock.now_iso()
        identifier = job_id or ids.job_id(role, subject, self.next_sequence(project_id, role, subject))
        row = {
            "id": identifier,
            "project_id": project_id,
            "goal_id": goal_id,
            "plan_id": plan_id,
            "job_type": job_type,
            "role": role,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "acceptance_criteria": [],
            "blockers": [],
            "metadata": {},
        }
        row.update(fields)
        row = _encode("jobs", row)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        with db.transaction(self.conn):
            self.conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", row)
            self.conn.execute(
                "INSERT INTO job_transitions (job_id, from_status, to_status, actor, reason,"
                " created_at) VALUES (?, NULL, ?, 'supervisor', 'created', ?)",
                (identifier, status, now),
            )
            for dependency in depends_on:
                self.conn.execute(
                    "INSERT OR IGNORE INTO job_dependencies (job_id, depends_on_job_id)"
                    " VALUES (?, ?)",
                    (identifier, dependency),
                )
        return self.get_job(identifier)

    def get_job(self, job_id):
        return decode("jobs", db.one(self.conn, "SELECT * FROM jobs WHERE id = ?", (job_id,)))

    def require_job(self, job_id):
        job = self.get_job(job_id)
        if job is None:
            raise NotFound(f"no such job: {job_id!r}")
        return job

    def list_jobs(self, project_id=None, goal_id=None, status=None, role=None):
        sql = "SELECT * FROM jobs WHERE 1=1"
        params = []
        for column, value in (("project_id", project_id), ("goal_id", goal_id), ("role", role)):
            if value:
                sql += f" AND {column} = ?"
                params.append(value)
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY priority DESC, created_at"
        return [decode("jobs", r) for r in db.all_rows(self.conn, sql, params)]

    def update_job(self, job_id, **fields):
        unknown = set(fields) - _JOB_WRITABLE
        if unknown:
            raise ValueError(f"unknown job fields: {sorted(unknown)}")
        if not fields:
            return self.get_job(job_id)
        encoded = _encode("jobs", fields)
        assignments = ", ".join(f"{k} = :{k}" for k in encoded)
        encoded["job_id"] = job_id
        encoded["updated_at"] = clock.now_iso()
        with db.transaction(self.conn):
            self.conn.execute(
                f"UPDATE jobs SET {assignments}, updated_at = :updated_at WHERE id = :job_id",
                encoded,
            )
        return self.get_job(job_id)

    # --- dependencies -----------------------------------------------------

    def add_dependency(self, job_id, depends_on_job_id):
        if job_id == depends_on_job_id:
            raise ValueError("a job cannot depend on itself")
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO job_dependencies (job_id, depends_on_job_id) VALUES (?, ?)",
                (job_id, depends_on_job_id),
            )

    def dependencies(self, job_id):
        return [
            r["depends_on_job_id"]
            for r in db.all_rows(
                self.conn,
                "SELECT depends_on_job_id FROM job_dependencies WHERE job_id = ? ORDER BY 1",
                (job_id,),
            )
        ]

    def dependents(self, job_id):
        return [
            r["job_id"]
            for r in db.all_rows(
                self.conn,
                "SELECT job_id FROM job_dependencies WHERE depends_on_job_id = ? ORDER BY 1",
                (job_id,),
            )
        ]

    def unmet_dependencies(self, job_id):
        return [
            r["depends_on_job_id"]
            for r in db.all_rows(
                self.conn,
                "SELECT d.depends_on_job_id FROM job_dependencies d"
                " JOIN jobs j ON j.id = d.depends_on_job_id"
                " WHERE d.job_id = ? AND j.status != 'DONE' ORDER BY 1",
                (job_id,),
            )
        ]

    def promote_ready(self, project_id=None, actor="scheduler"):
        """PLANNED jobs whose dependencies are all DONE become READY.

        Readiness is a SQL question, never a model's opinion.
        """
        sql = (
            "SELECT * FROM jobs j WHERE j.status = 'PLANNED'"
            " AND NOT EXISTS (SELECT 1 FROM job_dependencies d JOIN jobs dj"
            "   ON dj.id = d.depends_on_job_id"
            "   WHERE d.job_id = j.id AND dj.status != 'DONE')"
        )
        params = []
        if project_id:
            sql += " AND j.project_id = ?"
            params.append(project_id)
        promoted = []
        for row in db.all_rows(self.conn, sql + " ORDER BY priority DESC, created_at", params):
            promoted.append(
                self.transition(row["id"], machine.READY, actor=actor, reason="dependencies satisfied")
            )
        return promoted

    # --- transitions ------------------------------------------------------

    def transitions(self, job_id):
        return [
            dict(r)
            for r in db.all_rows(
                self.conn,
                "SELECT * FROM job_transitions WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
        ]

    def work_actors(self, job_id):
        """Who actually did the work on this job — the set an approver must not be in."""
        return {
            r["actor"]
            for r in db.all_rows(
                self.conn,
                "SELECT DISTINCT actor FROM job_transitions"
                " WHERE job_id = ? AND to_status IN ('RUNNING', 'WORK_COMPLETE')",
                (job_id,),
            )
        }

    def transition(self, job_id, to_status, actor, reason="", fields=None):
        """Move a job. Raises IllegalTransition or a guard error rather than repairing."""
        job = self.require_job(job_id)
        machine.check_transition(job_id, job["status"], to_status)
        self._run_guards(job, to_status, actor)

        updates = _encode("jobs", dict(fields or {}))
        unknown = set(updates) - _JOB_WRITABLE
        if unknown:
            raise ValueError(f"unknown job fields: {sorted(unknown)}")
        now = clock.now_iso()
        with db.transaction(self.conn):
            assignments = "".join(f", {k} = :{k}" for k in updates)
            params = dict(updates, job_id=job_id, status=to_status, updated_at=now)
            self.conn.execute(
                f"UPDATE jobs SET status = :status, updated_at = :updated_at{assignments}"
                " WHERE id = :job_id",
                params,
            )
            self.conn.execute(
                "INSERT INTO job_transitions (job_id, from_status, to_status, actor, reason,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, job["status"], to_status, actor, reason, now),
            )
        return self.get_job(job_id)

    def _run_guards(self, job, to_status, actor):
        guard = machine.GUARDED.get(to_status)
        if guard == "approval":
            machine.guard_approval(job, actor, self.work_actors(job["id"]))
        elif guard == "landing":
            machine.guard_landing(job)
        elif guard == "revision":
            machine.guard_revision(job)

    # --- relations --------------------------------------------------------

    def relate(self, job_id, related_job_id, kind, note=""):
        """Record DUPLICATE / SUPERSEDES / SUPERSEDED_BY. Nothing is ever deleted."""
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO job_relations (job_id, related_job_id, kind, note,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, related_job_id, kind, note, clock.now_iso()),
            )

    def relations(self, job_id):
        return [
            dict(r)
            for r in db.all_rows(
                self.conn,
                "SELECT * FROM job_relations WHERE job_id = ? OR related_job_id = ? ORDER BY created_at",
                (job_id, job_id),
            )
        ]

    # --- events -----------------------------------------------------------

    def record_event(self, kind, payload=None, idempotency_key=None, project_id=None, job_id=None):
        """Append an event. A repeated idempotency key is a no-op, not an error."""
        row = _encode(
            "events",
            {
                "idempotency_key": idempotency_key,
                "kind": kind,
                "project_id": project_id,
                "job_id": job_id,
                "payload": payload or {},
                "created_at": clock.now_iso(),
            },
        )
        try:
            with db.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO events (idempotency_key, kind, project_id, job_id, payload,"
                    " created_at) VALUES (:idempotency_key, :kind, :project_id, :job_id,"
                    " :payload, :created_at)",
                    row,
                )
        except sqlite3.IntegrityError:
            existing = db.one(
                self.conn, "SELECT * FROM events WHERE idempotency_key = ?", (idempotency_key,)
            )
            return decode("events", existing), False
        created = db.one(
            self.conn, "SELECT * FROM events ORDER BY id DESC LIMIT 1"
        )
        return decode("events", created), True

    def events(self, job_id=None, kind=None, limit=100):
        sql = "SELECT * FROM events WHERE 1=1"
        params = []
        if job_id:
            sql += " AND job_id = ?"
            params.append(job_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [decode("events", r) for r in db.all_rows(self.conn, sql, params)]
