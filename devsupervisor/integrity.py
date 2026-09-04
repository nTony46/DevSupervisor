"""Verify that a read-only worker was actually read-only.

A tool allowlist is a hope, not a guarantee. Prefix-matched shell entries cannot
enumerate every safe invocation — `PYTHONPATH=. python3 -m unittest` fails to
match `Bash(python3 -m unittest:*)` — so tightening the list either blocks real
work or leaks. The stronger property is checkable after the fact: the tree the
reviewer looked at is byte-identical to the tree it was given.
"""

from . import gitfacts
from .errors import PolicyViolation


def snapshot(worktree):
    """What must not change while a read-only worker runs."""
    if not worktree:
        return None
    facts = gitfacts.facts(worktree)
    if not facts.get("is_git"):
        return {"worktree": str(worktree), "is_git": False}
    return {
        "worktree": str(worktree),
        "is_git": True,
        "head": facts.get("head"),
        "branch": facts.get("branch"),
        "clean": facts.get("clean"),
        "dirty_paths": facts.get("dirty_paths"),
    }


def diff(before, after):
    if not before or not after:
        return {}
    return {key: (before.get(key), after.get(key))
            for key in ("head", "branch", "clean", "dirty_paths")
            if before.get(key) != after.get(key)}


def assert_unchanged(job_id, role, before, after):
    """Raise if a read-only role changed the checkout it was inspecting."""
    changes = diff(before, after)
    if not changes:
        return True
    detail = "; ".join(f"{key}: {was!r} -> {now!r}" for key, (was, now) in changes.items())
    raise PolicyViolation(
        f"job {job_id} ({role}) is read-only but its worktree changed ({detail})"
    )
