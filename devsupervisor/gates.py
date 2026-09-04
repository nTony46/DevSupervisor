"""Human decision gates.

A gate is a database row, not a question asked in a chat window. That is the
whole point: a gate confirmed in conversation leaves no evidence, and the
handoffs this system was built from contain landings whose preconditions were
"confirmed interactively" and could not afterwards be verified from the repo.
"""

from . import clock, ids
from .errors import NotFound
from .state import db, machine

KINDS = (
    "budget", "destructive", "benchmark_mutation", "strategy", "privacy",
    "api_tradeoff", "conflicting_evidence", "retries_exhausted", "protected_path",
)


def open_gate(store, kind, question, project_id=None, goal_id=None, job_id=None,
              context="", resume_status=machine.READY, actor="supervisor"):
    """Raise a gate and park the affected job. Returns the gate row."""
    gate_id = ids.new_id("gate")
    with db.transaction(store.conn):
        store.conn.execute(
            "INSERT INTO human_gates (id, project_id, goal_id, job_id, kind, question,"
            " context, status, resume_status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)",
            (gate_id, project_id, goal_id, job_id, kind, question, context,
             resume_status, clock.now_iso()),
        )
    if job_id:
        job = store.get_job(job_id)
        if job and job["status"] != machine.WAITING_HUMAN:
            store.transition(job_id, machine.WAITING_HUMAN, actor=actor,
                             reason=f"human gate: {kind}")
    store.record_event("gate.opened", {"gate_id": gate_id, "kind": kind},
                       project_id=project_id, job_id=job_id)
    return get(store, gate_id)


def get(store, gate_id):
    row = db.one(store.conn, "SELECT * FROM human_gates WHERE id = ?", (gate_id,))
    if row is None:
        raise NotFound(f"no such gate: {gate_id!r}")
    return dict(row)


def open_gates(store, project_id=None, job_id=None):
    sql = "SELECT * FROM human_gates WHERE status = 'OPEN'"
    params = []
    for column, value in (("project_id", project_id), ("job_id", job_id)):
        if value:
            sql += f" AND {column} = ?"
            params.append(value)
    return [dict(r) for r in db.all_rows(store.conn, sql + " ORDER BY created_at", params)]


def decide(store, gate_id, approved, actor, note=""):
    """Record the decision and unpark the job. Only this releases WAITING_HUMAN."""
    gate = get(store, gate_id)
    if gate["status"] != "OPEN":
        return gate
    status = "APPROVED" if approved else "REJECTED"
    with db.transaction(store.conn):
        store.conn.execute(
            "UPDATE human_gates SET status = ?, decided_by = ?, decision_note = ?,"
            " decided_at = ? WHERE id = ?",
            (status, actor, note, clock.now_iso(), gate_id),
        )
    if gate["job_id"]:
        job = store.get_job(gate["job_id"])
        if job and job["status"] == machine.WAITING_HUMAN:
            remaining = [g for g in open_gates(store, job_id=job["id"])]
            if not remaining:
                target = gate["resume_status"] if approved else machine.BLOCKED
                store.transition(job["id"], target, actor=actor,
                                 reason=f"gate {status.lower()} by {actor}")
    store.record_event("gate.decided", {"gate_id": gate_id, "status": status, "actor": actor},
                       project_id=gate["project_id"], job_id=gate["job_id"])
    return get(store, gate_id)


def has_approval(store, job_id, kind=None):
    sql = "SELECT COUNT(*) AS n FROM human_gates WHERE job_id = ? AND status = 'APPROVED'"
    params = [job_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return db.one(store.conn, sql, params)["n"] > 0
