"""Learnable orchestration policy: versioned, reviewable, never self-adopting.

Everything here is a proposal until a named actor adopts it. The immutable
safety rules are not reachable from this module at all — attempting to propose
one raises.
"""

import json

from .. import clock, ids
from ..errors import NotFound, PolicyViolation
from ..state import db
from ..state.store import decode
from . import immutable

# Orchestration decisions a retrospective is allowed to influence.
LEARNABLE_KINDS = (
    "decomposition", "review_depth", "model_choice", "concurrency",
    "retry_strategy", "session_reuse", "prompt_template",
)

_RESERVED_PREFIXES = ("immutable", "safety", "hard_rule")


def _guard_name(name, kind):
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in _RESERVED_PREFIXES):
        raise PolicyViolation(
            f"policy {name!r} targets immutable safety rules, which are not learnable"
        )
    if kind not in LEARNABLE_KINDS:
        raise PolicyViolation(
            f"policy kind {kind!r} is not learnable; expected one of {LEARNABLE_KINDS}"
        )


def propose(store, name, kind, body, rationale="", evidence=None):
    """Record a candidate policy. Adoption is a separate, named act."""
    _guard_name(name, kind)
    if kind == "model_choice":
        # Refuse at proposal time, so a downgrade never reaches a reviewer
        # looking like an ordinary optimisation.
        immutable.check_routing_policy(body)
    row = db.one(store.conn, "SELECT MAX(version) AS v FROM policies WHERE name = ?", (name,))
    version = (row["v"] or 0) + 1
    policy_id = ids.new_id("pol")
    with db.transaction(store.conn):
        store.conn.execute(
            "INSERT INTO policies (id, name, version, kind, body, status, rationale,"
            " evidence, created_at) VALUES (?, ?, ?, ?, ?, 'CANDIDATE', ?, ?, ?)",
            (policy_id, name, version, kind, json.dumps(body), rationale,
             json.dumps(evidence or {}), clock.now_iso()),
        )
    return get(store, policy_id)


def adopt(store, policy_id, actor):
    """Promote a candidate. The previously adopted version is superseded, not deleted."""
    policy = get(store, policy_id)
    if policy["status"] != "CANDIDATE":
        raise PolicyViolation(f"policy {policy_id} is {policy['status']}, not a candidate")
    with db.transaction(store.conn):
        store.conn.execute(
            "UPDATE policies SET status = 'SUPERSEDED' WHERE name = ? AND status = 'ADOPTED'",
            (policy["name"],),
        )
        store.conn.execute(
            "UPDATE policies SET status = 'ADOPTED', adopted_at = ?, adopted_by = ? WHERE id = ?",
            (clock.now_iso(), actor, policy_id),
        )
    return get(store, policy_id)


def reject(store, policy_id, actor, note=""):
    policy = get(store, policy_id)
    with db.transaction(store.conn):
        store.conn.execute(
            "UPDATE policies SET status = 'REJECTED', adopted_by = ?, rationale = ? WHERE id = ?",
            (actor, (policy["rationale"] + f"\nrejected: {note}").strip(), policy_id),
        )
    return get(store, policy_id)


def get(store, policy_id):
    row = db.one(store.conn, "SELECT * FROM policies WHERE id = ?", (policy_id,))
    if row is None:
        raise NotFound(f"no such policy: {policy_id!r}")
    return decode("policies", row)


def active(store, name):
    row = db.one(
        store.conn,
        "SELECT * FROM policies WHERE name = ? AND status = 'ADOPTED'"
        " ORDER BY version DESC LIMIT 1",
        (name,),
    )
    return decode("policies", row)


def effective(store, name, default):
    """The adopted body for a policy, or the supplied default."""
    policy = active(store, name)
    return policy["body"] if policy else default


def list_policies(store, status=None, kind=None):
    sql = "SELECT * FROM policies WHERE 1=1"
    params = []
    for column, value in (("status", status), ("kind", kind)):
        if value:
            sql += f" AND {column} = ?"
            params.append(value)
    return [decode("policies", r)
            for r in db.all_rows(store.conn, sql + " ORDER BY name, version", params)]


def immutable_rules():
    """Read-only passthrough, so callers never reach for a mutable copy."""
    return immutable.rules()
