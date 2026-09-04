"""Memory candidates.

A worker's claim is a hypothesis. It lands here, not in authoritative memory,
and becomes durable only through an explicit adoption that is recorded.
"""

from .. import clock, config, ids
from ..errors import NotFound
from . import documents, redact
from .store import MemoryStore

ADOPTED_DIRNAME = "adopted"


def _directory(project_id):
    path = config.memory_candidates_dir(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def propose(project_id, title, body, area, tags=(), source_job=None, kind="fact"):
    """Record a proposed memory. Secrets are stripped rather than refused here,
    because a candidate is exactly where an unreviewed claim belongs."""
    directory = _directory(project_id)
    stamp = clock.now().strftime("%Y%m%dT%H%M%S")
    name = f"{stamp}-{ids.slug(title, max_words=8)}.md"
    meta = documents.new_meta(
        title, area, tags, source_job, project=project_id,
        extra={"kind": kind, "status": "PROPOSED"},
    )
    target = directory / name
    target.write_text(documents.render(meta, redact.redact(body)))
    return target


def list_candidates(project_id, include_adopted=False):
    directory = _directory(project_id)
    out = []
    paths = sorted(directory.glob("*.md"))
    if include_adopted:
        paths += sorted((directory / ADOPTED_DIRNAME).glob("*.md"))
    for path in paths:
        meta, body = documents.parse(path.read_text())
        out.append({
            "path": str(path),
            "name": path.name,
            "title": meta.get("title", path.stem),
            "area": meta.get("area", "lessons"),
            "tags": meta.get("tags", []),
            "kind": meta.get("kind", "fact"),
            "status": meta.get("status", "PROPOSED"),
            "source_job": meta.get("source_job"),
            "body": body,
        })
    return out


def get(project_id, name):
    for candidate in list_candidates(project_id, include_adopted=True):
        if candidate["name"] == name:
            return candidate
    raise NotFound(f"no memory candidate named {name!r}")


def adopt(project_id, name, actor, overwrite=False, slug=None):
    """Promote a candidate into authoritative memory. Explicit act, audited."""
    candidate = get(project_id, name)
    memory = MemoryStore(project_id)
    written = memory.write(
        candidate["area"], candidate["title"], candidate["body"],
        tags=candidate["tags"], source_job=candidate["source_job"],
        slug=slug, overwrite=overwrite,
    )
    source = _directory(project_id) / name
    archive = _directory(project_id) / ADOPTED_DIRNAME
    archive.mkdir(parents=True, exist_ok=True)
    meta, body = documents.parse(source.read_text())
    meta.update({"status": "ADOPTED", "adopted_by": actor, "adopted_at": clock.now_iso(),
                 "adopted_path": str(written)})
    (archive / name).write_text(documents.render(meta, body))
    source.unlink()
    return written
