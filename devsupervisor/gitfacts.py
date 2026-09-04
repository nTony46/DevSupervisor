"""Read-only git inspection.

Ground truth about a repository, gathered by running git rather than by asking
a worker what it thinks the branch is. Every function here is read-only; there
is no code path in this module that writes to a repository.
"""

import subprocess
from pathlib import Path

TIMEOUT = 30


def _git(repo, *args):
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if completed.returncode != 0:
        return None, (completed.stderr or "").strip()
    return completed.stdout.strip(), None


def is_repo(repo):
    value, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    return value == "true"


def facts(repo):
    """The facts a job packet should state about a checkout."""
    path = Path(repo).expanduser()
    if not path.exists():
        return {"repo": str(path), "exists": False, "is_git": False}
    if not is_repo(path):
        return {"repo": str(path), "exists": True, "is_git": False,
                "note": "not a git repository: SHA-based review and landing do not apply"}

    branch, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    head, _ = _git(path, "rev-parse", "HEAD")
    status, _ = _git(path, "status", "--porcelain")
    upstream, _ = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    toplevel, _ = _git(path, "rev-parse", "--show-toplevel")
    origin, _ = _git(path, "config", "--get", "remote.origin.url")
    return {
        "repo": str(path), "exists": True, "is_git": True, "toplevel": toplevel,
        "branch": branch, "head": head, "upstream": upstream, "origin": origin,
        "clean": not bool(status), "dirty_paths": len((status or "").splitlines()),
    }


def commit_exists(repo, sha):
    value, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    return value is not None


def is_ancestor(repo, sha, ref):
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, ref],
            capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.returncode == 0


def rev_parse(repo, ref):
    value, _ = _git(repo, "rev-parse", ref)
    return value


def worktrees(repo):
    """Every registered worktree, so two jobs are not pointed at the same one."""
    value, _ = _git(repo, "worktree", "list", "--porcelain")
    if not value:
        return []
    entries, current = [], {}
    for line in value.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, rest = line.partition(" ")
        current[key] = rest
    if current:
        entries.append(current)
    return entries
