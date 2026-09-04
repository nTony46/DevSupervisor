"""Parse agent handoff documents into structured facts.

Handoffs are prose written by whoever is about to disappear. The parser pulls
out only what can be checked later — repo, branch, SHAs, claimed status — and
leaves the narrative as body text. Anything credential-shaped is redacted on the
way in, because an imported handoff becomes durable.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..memory import redact

_SHA = re.compile(r"\b([0-9a-f]{7,40})\b")
_TABLE_ROW = re.compile(r"^\s*\|\s*(?:\*\*)?([A-Za-z][A-Za-z /]*?)(?:\*\*)?\s*\|\s*(.+?)\s*\|\s*$")
_BOLD_FIELD = re.compile(r"^\s*[-*]\s*\*\*([A-Za-z][A-Za-z /]*?):?\*\*\s*(.+?)\s*$")
_BACKTICKED = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^#+\s*(.+?)\s*$")

# Values that mean "no value". Handoffs write these where a field is absent.
_EMPTY_VALUES = ("n/a", "none", "-", "(none)", "unknown", "not applicable", "")

# Generic section headings that are not a document's subject.
_GENERIC_HEADINGS = (
    "agent / role", "agent/role", "current status", "task / goal", "task/goal",
    "files created", "role", "blockers", "work completed",
)

_STATUS_WORDS = (
    "COMPLETE", "ACTIVE", "WAITING", "BLOCKED", "PARKED", "INTERRUPTED",
    "DONE", "FAILED", "UNKNOWN",
)

# Field names as they appear in handoffs, mapped to what we call them.
_FIELD_ALIASES = {
    "repo": "repo", "repository": "repo",
    "branch": "branch",
    "head": "head", "tip": "head",
    "base": "base",
    "status": "worktree_status",
    "pushed": "pushed",
    "merged": "merged",
    "absolute path": "worktree", "worktree": "worktree", "primary worktree": "worktree",
    "role": "role", "agent number": "agent_label", "agent": "agent_label",
}


@dataclass
class Handoff:
    path: str
    name: str
    title: str = ""
    heading: str = ""
    agent_label: str = ""
    role: str = ""
    status: str = "UNKNOWN"
    repo: str = None
    branch: str = None
    head: str = None
    base: str = None
    worktree: str = None
    pushed: bool = None
    merged: bool = None
    shas: list = field(default_factory=list)
    branches: list = field(default_factory=list)
    body: str = ""
    status_text: str = ""

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != "body"}


def _clean(value):
    """Strip markdown emphasis and backticks from a table cell.

    Returns None for the several ways a handoff writes "there is no value here".
    """
    value = value.strip()
    match = _BACKTICKED.search(value)
    cleaned = match.group(1).strip() if match else value.strip("* ").strip()
    if cleaned.lower().split("—")[0].strip() in _EMPTY_VALUES:
        return None
    return cleaned or None


def _truthy(value):
    lowered = (value or "").lower()
    if any(word in lowered for word in ("yes", "true", "merged")) and "not " not in lowered:
        return True
    if any(word in lowered for word in ("no", "false", "unmerged", "not merged")):
        return False
    return None


def parse_text(text, name="handoff", path=""):
    text = redact.redact(text)
    handoff = Handoff(path=path, name=name, body=text,
                      title=_title_from_name(name))

    lines = text.splitlines()
    section = ""
    fields = {}
    status_lines = []

    for line in lines:
        heading = _HEADING.match(line)
        if heading and line.lstrip().startswith("#"):
            section = heading.group(1).strip().lower()
            if not handoff.heading and section not in _GENERIC_HEADINGS:
                handoff.heading = heading.group(1).strip()
            continue

        if section.startswith("current status") and line.strip():
            status_lines.append(line.strip())

        for pattern in (_TABLE_ROW, _BOLD_FIELD):
            match = pattern.match(line)
            if not match:
                continue
            key = _FIELD_ALIASES.get(match.group(1).strip().lower())
            if key and key not in fields:
                fields[key] = _clean(match.group(2))
            break

    handoff.status_text = " ".join(status_lines[:3])
    handoff.status = _status_of(handoff.status_text)
    handoff.repo = fields.get("repo")
    handoff.branch = fields.get("branch")
    handoff.head = _sha_or_none(fields.get("head"))
    handoff.base = _sha_or_none(fields.get("base"))
    handoff.worktree = fields.get("worktree")
    handoff.role = fields.get("role", "")
    handoff.agent_label = fields.get("agent_label", "")
    handoff.pushed = _truthy(fields.get("pushed"))
    handoff.merged = _truthy(fields.get("merged"))
    handoff.shas = sorted({m.group(1) for m in _SHA.finditer(text) if len(m.group(1)) >= 7})
    handoff.branches = sorted({
        value for value in _BACKTICKED.findall(text)
        if "/" in value and " " not in value and not value.startswith(("/", "~", "."))
        and not value.endswith((".md", ".rs", ".py", ".json", ".sh", ".toml"))
    })
    return handoff


def _title_from_name(name):
    """A handoff's file name is its most reliable subject, unglamorous as that is."""
    stem = name[:-3] if name.endswith(".md") else name
    return stem.replace("-", " ").replace("_", " ").strip()


def _sha_or_none(value):
    if not value:
        return None
    match = _SHA.search(value)
    return match.group(1) if match else None


def _status_of(text):
    upper = (text or "").upper()
    for word in _STATUS_WORDS:
        if word in upper:
            return word
    return "UNKNOWN"


def parse(path):
    path = Path(path)
    return parse_text(path.read_text(errors="replace"), name=path.name, path=str(path))


def load_all(directory):
    directory = Path(directory)
    if not directory.exists():
        return []
    return [parse(path) for path in sorted(directory.glob("*.md"))]
