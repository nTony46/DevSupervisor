"""The artifact registry.

Workers communicate through durable artifacts — commits, diffs, reports, test
logs, manifests, JSON results — and downstream jobs receive references plus a
curated summary rather than a copy of the upstream conversation. This is what
lets a fresh agent continue after a context reset.
"""

import hashlib

from . import clock, config, ids
from .errors import NotFound
from .memory import redact
from .state import db
from .state.store import decode

KINDS = (
    "commit", "diff", "report", "test_log", "design", "manifest",
    "json_result", "spec", "review", "landing_report", "transcript",
)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def register(store, project_id, kind, uri, job_id=None, summary="", sha256=None, metadata=None):
    """Record an artifact that already exists (a commit sha, a file on disk)."""
    artifact_id = ids.new_id("art")
    row = {
        "id": artifact_id,
        "project_id": project_id,
        "job_id": job_id,
        "kind": kind,
        "uri": str(uri),
        "sha256": sha256,
        "summary": redact.redact(summary),
        "metadata": __import__("json").dumps(metadata or {}),
        "created_at": clock.now_iso(),
    }
    with db.transaction(store.conn):
        store.conn.execute(
            "INSERT INTO artifacts (id, project_id, job_id, kind, uri, sha256, summary,"
            " metadata, created_at) VALUES (:id, :project_id, :job_id, :kind, :uri, :sha256,"
            " :summary, :metadata, :created_at)",
            row,
        )
    return get(store, artifact_id)


def write(store, project_id, job_id, name, content, kind, summary=""):
    """Persist text as a file under the project's artifacts dir, then register it."""
    directory = config.artifacts_dir(project_id) / (job_id or "unassigned")
    directory.mkdir(parents=True, exist_ok=True)
    safe = redact.redact(content)
    path = directory / name
    path.write_text(safe)
    return register(
        store, project_id, kind, str(path), job_id=job_id, summary=summary,
        sha256=sha256_text(safe),
    )


def get(store, artifact_id):
    row = db.one(store.conn, "SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if row is None:
        raise NotFound(f"no such artifact: {artifact_id!r}")
    return decode("artifacts", row)


def for_job(store, job_id, kind=None):
    sql = "SELECT * FROM artifacts WHERE job_id = ?"
    params = [job_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return [decode("artifacts", r)
            for r in db.all_rows(store.conn, sql + " ORDER BY created_at", params)]


def reference(artifact):
    """One packet-sized line: what it is, where it is, and how to verify it."""
    parts = [f"[{artifact['kind']}] {artifact['uri']}"]
    if artifact.get("sha256"):
        parts.append(f"sha256={artifact['sha256'][:12]}")
    if artifact.get("summary"):
        parts.append(f"— {artifact['summary']}")
    return " ".join(parts)
