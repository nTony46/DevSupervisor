"""Job ownership leases and the single-owner supervisor lock.

Parallelism is only useful when workers are solving distinct jobs. A lease is
what stops two workers from silently solving the same one — and when a lease is
reclaimed, the reclaim is an event, so the duplicate risk is auditable instead
of invisible.
"""

import os
import sqlite3

from .. import clock, ids
from ..errors import LeaseError
from . import db, machine

DEFAULT_TTL_SECONDS = 900
SUPERVISOR_LOCK_TTL_SECONDS = 300


def holder(store, job_id):
    row = db.one(store.conn, "SELECT * FROM leases WHERE job_id = ?", (job_id,))
    return dict(row) if row else None


def is_expired(lease, at=None):
    return clock.parse(lease["expires_at"]) <= (at or clock.now())


def acquire(store, job_id, owner, ttl_seconds=DEFAULT_TTL_SECONDS):
    """Take ownership of a job. Returns a token; raises if someone else holds it."""
    now = clock.now()
    token = ids.new_id("lease")
    existing = holder(store, job_id)
    if existing and not is_expired(existing, now):
        if existing["owner"] == owner:
            return existing["token"]
        raise LeaseError(
            f"job {job_id} is leased by {existing['owner']!r} until {existing['expires_at']}"
        )
    expires = clock.iso(clock.plus_seconds(ttl_seconds))
    try:
        with db.transaction(store.conn):
            store.conn.execute(
                "INSERT OR REPLACE INTO leases (job_id, owner, token, acquired_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, owner, token, clock.iso(now), expires),
            )
    except sqlite3.IntegrityError as exc:  # pragma: no cover - defensive
        raise LeaseError(f"could not lease job {job_id}: {exc}") from exc
    return token


def renew(store, job_id, token, ttl_seconds=DEFAULT_TTL_SECONDS):
    lease = holder(store, job_id)
    if not lease or lease["token"] != token:
        raise LeaseError(f"job {job_id}: not the lease holder")
    with db.transaction(store.conn):
        store.conn.execute(
            "UPDATE leases SET expires_at = ? WHERE job_id = ?",
            (clock.iso(clock.plus_seconds(ttl_seconds)), job_id),
        )


def release(store, job_id, token=None):
    lease = holder(store, job_id)
    if lease and token is not None and lease["token"] != token:
        raise LeaseError(f"job {job_id}: not the lease holder")
    with db.transaction(store.conn):
        store.conn.execute("DELETE FROM leases WHERE job_id = ?", (job_id,))


def reclaim_expired(store, actor="scheduler"):
    """Recover jobs whose worker died. Returns the job ids reclaimed."""
    now = clock.now()
    reclaimed = []
    for row in db.all_rows(store.conn, "SELECT * FROM leases"):
        lease = dict(row)
        if not is_expired(lease, now):
            continue
        job = store.get_job(lease["job_id"])
        release(store, lease["job_id"])
        store.record_event(
            "lease.reclaimed",
            payload={"job_id": lease["job_id"], "previous_owner": lease["owner"]},
            job_id=lease["job_id"],
        )
        if job and job["status"] in (machine.DISPATCHED, machine.RUNNING):
            store.transition(
                job["id"], machine.READY, actor=actor,
                reason=f"lease expired (owner {lease['owner']})",
            )
        reclaimed.append(lease["job_id"])
    return reclaimed


# --- supervisor single-owner lock -----------------------------------------


def acquire_supervisor_lock(store, owner, ttl_seconds=SUPERVISOR_LOCK_TTL_SECONDS):
    """Only one supervisor loop may drive a runtime root at a time."""
    now = clock.now()
    row = db.one(store.conn, "SELECT * FROM supervisor_lock WHERE id = 1")
    if row and clock.parse(row["expires_at"]) > now and row["owner"] != owner:
        raise LeaseError(
            f"supervisor lock held by {row['owner']!r} (pid {row['pid']}) until {row['expires_at']}"
        )
    with db.transaction(store.conn):
        store.conn.execute(
            "INSERT OR REPLACE INTO supervisor_lock (id, owner, pid, acquired_at, expires_at)"
            " VALUES (1, ?, ?, ?, ?)",
            (owner, os.getpid(), clock.iso(now), clock.iso(clock.plus_seconds(ttl_seconds))),
        )
    return owner


def release_supervisor_lock(store, owner):
    with db.transaction(store.conn):
        store.conn.execute("DELETE FROM supervisor_lock WHERE id = 1 AND owner = ?", (owner,))


def supervisor_lock_status(store):
    row = db.one(store.conn, "SELECT * FROM supervisor_lock WHERE id = 1")
    if not row:
        return None
    lock = dict(row)
    lock["expired"] = clock.parse(lock["expires_at"]) <= clock.now()
    return lock
