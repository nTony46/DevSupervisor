"""Dispatching independent jobs at the same time.

The scheduler has carried a `concurrency` setting since the beginning and never
used it: every dispatch was serial. Two independent reviews of two different
SHAs in two different worktrees have no reason to queue behind each other.

Each worker thread gets its own database connection, because a SQLite connection
is not safe to share across threads. Leases are per job, so two threads cannot
take the same work; WAL plus the busy timeout handles the concurrent writes.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from .state import Store, leases


def dispatch_parallel(project_id, job_ids, supervisor_factory, owner="supervisor-1",
                      lock=True):
    """Run each job in its own thread, with its own store. Returns per-job outcomes.

    `supervisor_factory(store)` builds a Supervisor bound to that thread's store,
    so the caller keeps control of provider, pack, and routing.
    """
    results = {}
    lock_store = Store.open() if lock else None
    if lock_store is not None:
        leases.acquire_supervisor_lock(lock_store, owner)

    def run_one(job_id):
        store = Store.open()
        try:
            supervisor = supervisor_factory(store)
            job = store.require_job(job_id)
            advanced = supervisor.advance(job)
            return {"job_id": job_id, "advanced": advanced,
                    "status": store.get_job(job_id)["status"]}
        except BaseException as exc:                     # report, never swallow
            return {"job_id": job_id, "error": f"{type(exc).__name__}: {exc}",
                    "status": None}
        finally:
            store.close()

    try:
        with ThreadPoolExecutor(max_workers=max(1, len(job_ids))) as pool:
            for outcome in pool.map(run_one, job_ids):
                results[outcome["job_id"]] = outcome
    finally:
        if lock_store is not None:
            leases.release_supervisor_lock(lock_store, owner)
            lock_store.close()
    return results


def ready_for_parallel(store, project_id, limit=None):
    """Ready jobs that do not share a worktree.

    Two jobs in one checkout are not independent however ready they both look.
    """
    from .scheduler import Scheduler
    seen, selected = set(), []
    for job in store.list_jobs(project_id, status="READY"):
        key = job["worktree"] or job["repo"] or job["id"]
        if key in seen:
            continue
        seen.add(key)
        selected.append(job)
        if limit and len(selected) >= limit:
            break
    return selected
