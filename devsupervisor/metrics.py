"""Runs and measurements.

Every dispatch produces a run row; every run produces metric rows. This is the
raw material the retrospective reads, so it is recorded even when nobody is
looking at it yet.
"""

import json

from . import clock, ids
from .state import db
from .state.store import decode


def start_run(store, job, provider, model=None, session_id=None, prompt_version=None,
              routing=None, tools=None):
    """Open a run row. The routing decision is recorded here, not inferred later:
    a run whose model and effort are unknown cannot be compared to anything."""
    run_id = ids.new_id("run")
    routing = routing.to_dict() if hasattr(routing, "to_dict") else (routing or {})
    with db.transaction(store.conn):
        store.conn.execute(
            "INSERT INTO runs (id, job_id, attempt, role, provider, model, effort,"
            " routing_source, tools, max_budget_usd, session_id, prompt_version, status,"
            " started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)",
            (run_id, job["id"], job["attempt"], job["role"], provider,
             model or routing.get("model_id"), routing.get("effort"),
             routing.get("source"), json.dumps(list(tools or [])),
             routing.get("max_budget_usd"), session_id, prompt_version, clock.now_iso()),
        )
    return get_run(store, run_id)


def finish_run(store, run_id, outcome):
    run = get_run(store, run_id)
    started = clock.parse(run["started_at"])
    ended = clock.now()
    duration = (ended - started).total_seconds()
    payload = outcome.result.to_json() if outcome.result is not None else "{}"
    verdict = getattr(outcome.result, "verdict", None) if outcome.result else None
    with db.transaction(store.conn):
        store.conn.execute(
            "UPDATE runs SET status = ?, ended_at = ?, duration_s = ?, exit_code = ?,"
            " tokens_in = ?, tokens_out = ?, cost_usd = ?, transcript_path = ?,"
            " model_resolved = COALESCE(?, model_resolved),"
            " review_outcome = COALESCE(?, review_outcome),"
            " session_id = COALESCE(?, session_id), result = ? WHERE id = ?",
            (outcome.status, clock.iso(ended), duration, outcome.exit_code,
             outcome.tokens_in, outcome.tokens_out, outcome.cost_usd,
             outcome.transcript_path, getattr(outcome, "model_resolved", None),
             verdict, outcome.session_id, payload, run_id),
        )
    record(store, "run.duration_s", value=duration, job_id=run["job_id"], run_id=run_id)
    if outcome.cost_usd is not None:
        record(store, "run.cost_usd", value=outcome.cost_usd, job_id=run["job_id"], run_id=run_id)
    for name, value in (("run.tokens_in", outcome.tokens_in),
                        ("run.tokens_out", outcome.tokens_out)):
        if value is not None:
            record(store, name, value=value, job_id=run["job_id"], run_id=run_id)
    return get_run(store, run_id)


def get_run(store, run_id):
    return decode("runs", db.one(store.conn, "SELECT * FROM runs WHERE id = ?", (run_id,)))


def runs_for(store, job_id):
    return [decode("runs", r) for r in db.all_rows(
        store.conn, "SELECT * FROM runs WHERE job_id = ? ORDER BY started_at", (job_id,))]


def record(store, name, value=None, text=None, job_id=None, run_id=None, project_id=None):
    with db.transaction(store.conn):
        store.conn.execute(
            "INSERT INTO metrics (project_id, job_id, run_id, name, value, text_value,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, job_id, run_id, name, value, text, clock.now_iso()),
        )


def values(store, name, project_id=None):
    sql = "SELECT * FROM metrics WHERE name = ?"
    params = [name]
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    return [dict(r) for r in db.all_rows(store.conn, sql + " ORDER BY id", params)]


def counts(store, name):
    """text_value -> count, for taxonomies like blocker categories."""
    rows = db.all_rows(
        store.conn,
        "SELECT text_value, COUNT(*) AS n FROM metrics WHERE name = ? GROUP BY text_value",
        (name,),
    )
    return {r["text_value"]: r["n"] for r in rows}


def total_cost(store, project_id=None):
    sql = "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM runs"
    params = []
    if project_id:
        sql += (" WHERE job_id IN (SELECT id FROM jobs WHERE project_id = ?)")
        params.append(project_id)
    return db.one(store.conn, sql, params)["total"]
