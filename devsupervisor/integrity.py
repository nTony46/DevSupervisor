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
        # Tracked modifications are the invariant. A reviewer that runs the
        # project's test suite legitimately leaves build output behind, and
        # failing a good review over a __pycache__ directory would teach the
        # wrong lesson — that running the checks is risky.
        "tracked_modified": gitfacts.modified_tracked(worktree),
        "untracked_count": len(gitfacts.untracked(worktree)),
    }


# Changing any of these means the worker altered committed state or tracked
# files. Untracked additions are reported, never enforced.
ENFORCED_KEYS = ("head", "branch", "tracked_modified")


def diff(before, after):
    if not before or not after:
        return {}
    return {key: (before.get(key), after.get(key))
            for key in ENFORCED_KEYS
            if before.get(key) != after.get(key)}


def side_effects(before, after):
    """Untracked files a run left behind. Informational."""
    if not before or not after:
        return 0
    return (after.get("untracked_count") or 0) - (before.get("untracked_count") or 0)


def assert_unchanged(job_id, role, before, after):
    """Raise if a read-only role changed the checkout it was inspecting."""
    changes = diff(before, after)
    if not changes:
        return True
    detail = "; ".join(f"{key}: {was!r} -> {now!r}" for key, (was, now) in changes.items())
    raise PolicyViolation(
        f"job {job_id} ({role}) is read-only but its worktree changed ({detail})"
    )
