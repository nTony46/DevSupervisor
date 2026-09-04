"""The on-disk memory document format.

Small, path-addressed Markdown with a minimal frontmatter block. Deliberately
not YAML: one dependency-free format that a human can read and edit, and that
cannot execute anything on load.
"""

from .. import clock

_DELIMITER = "---"
_LIST_FIELDS = ("tags",)


def render(meta, body):
    lines = [_DELIMITER]
    for key, value in meta.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        lines.append(f"{key}: {value}")
    lines.append(_DELIMITER)
    lines.append("")
    lines.append(body.rstrip() + "\n")
    return "\n".join(lines)


def parse(text):
    """-> (meta dict, body). A document without frontmatter is all body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        return {}, text
    meta = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _DELIMITER:
            body = "\n".join(lines[index + 1:]).lstrip("\n")
            return meta, body
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in _LIST_FIELDS:
            value = [part.strip() for part in value.split(",") if part.strip()]
        meta[key] = value
    return meta, ""


def new_meta(title, area, tags=(), source_job=None, project=None, extra=None,
             packet_visible=True):
    meta = {
        "title": title,
        "area": area,
        "tags": list(tags),
        "created": clock.now_iso(),
        # Some memory is for humans and planners, not for a worker doing today's
        # job. A roadmap hypothesis marked "do not implement" is exactly the kind
        # of thing that should never appear in a builder's packet.
        "packet_visible": "true" if packet_visible else "false",
    }
    if project:
        meta["project"] = project
    if source_job:
        meta["source_job"] = source_job
    meta.update(extra or {})
    return meta
