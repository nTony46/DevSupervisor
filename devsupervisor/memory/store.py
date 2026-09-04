"""Durable semantic memory: small path-addressed documents, one topic each.

A single growing MEMORY.md becomes unreadable to both humans and the relevance
scorer, so memory is a directory tree and every document is cited by path in
whatever packet includes it.
"""

import re
from pathlib import Path

from .. import config, ids
from ..errors import DevSupervisorError, NotFound
from . import documents, redact

_WORD = re.compile(r"[a-z0-9_]+")

# Weights for relevance: what a document is about beats what it happens to say.
_TITLE_WEIGHT = 3.0
_TAG_WEIGHT = 2.0
_BODY_WEIGHT = 1.0


class MemoryWriteRefused(DevSupervisorError):
    """A write would overwrite authoritative memory, or carried a secret."""


def _tokens(text):
    return set(_WORD.findall((text or "").lower()))


class MemoryStore:
    """Authoritative project memory. Workers propose; only curation adopts."""

    def __init__(self, project_id):
        self.project_id = project_id
        self.root = config.memory_dir(project_id)

    def path(self, area, slug):
        return self.root / area / f"{slug}.md"

    def write(self, area, title, body, tags=(), source_job=None, slug=None,
              overwrite=False, packet_visible=True):
        if area not in config.MEMORY_AREAS:
            raise ValueError(f"unknown memory area {area!r}; expected one of {config.MEMORY_AREAS}")
        if redact.contains_secret(body) or redact.contains_secret(title):
            raise MemoryWriteRefused(
                f"refusing to write memory {title!r}: content looks like a credential"
            )
        target = self.path(area, slug or ids.slug(title, max_words=8))
        if target.exists() and not overwrite:
            raise MemoryWriteRefused(
                f"{target} already exists; authoritative memory is never silently overwritten"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = documents.new_meta(title, area, tags, source_job, project=self.project_id,
                                  packet_visible=packet_visible)
        target.write_text(documents.render(meta, body))
        return target

    def read(self, area, slug):
        target = self.path(area, slug)
        if not target.exists():
            raise NotFound(f"no memory document at {target}")
        return self._load(target)

    def _load(self, path):
        meta, body = documents.parse(path.read_text())
        return {
            "path": str(path),
            "relative_path": str(path.relative_to(self.root.parent)),
            "area": meta.get("area", path.parent.name),
            "slug": path.stem,
            "title": meta.get("title", path.stem),
            "tags": meta.get("tags", []),
            "source_job": meta.get("source_job"),
            "created": meta.get("created"),
            "packet_visible": str(meta.get("packet_visible", "true")).lower() != "false",
            "body": body,
        }

    def list(self, area=None):
        areas = [area] if area else list(config.MEMORY_AREAS)
        found = []
        for name in areas:
            directory = self.root / name
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                found.append(self._load(path))
        return found

    def search(self, query_terms, limit=6, areas=None, include_hidden=False):
        """Deterministic term-overlap relevance. Ties break on path for stability."""
        wanted = _tokens(" ".join(query_terms))
        if not wanted:
            return []
        scored = []
        for doc in self.list():
            if areas and doc["area"] not in areas:
                continue
            if not include_hidden and not doc.get("packet_visible", True):
                continue
            score = (
                _TITLE_WEIGHT * len(wanted & _tokens(doc["title"]))
                + _TAG_WEIGHT * len(wanted & _tokens(" ".join(doc["tags"])))
                + _BODY_WEIGHT * len(wanted & _tokens(doc["body"]))
            )
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
        return [doc for _, doc in scored[:limit]]
