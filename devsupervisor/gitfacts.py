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


def changed_paths(repo, sha, base=None):
    """Files a branch tip changed relative to its merge base with the target."""
    base = base or merge_base(repo, sha, "HEAD")
    if not base:
        return []
    value, _ = _git(repo, "diff", "--name-only", f"{base}..{sha}")
    return [line for line in (value or "").splitlines() if line]


def merge_base(repo, left, right):
    value, _ = _git(repo, "merge-base", left, right)
    return value


def landed_by_content(repo, sha, main_ref="origin/main", against=None):
    """Is this branch's work already in main under a different commit?

    A branch that is not an ancestor of main is usually pending. Sometimes it is
    historical instead: the same change landed under a different SHA. That is a
    checkable claim — diff the files the branch touched between its tip and the
    target — and it must be checked, because re-landing already-landed work is
    one of the more expensive mistakes available.

    Pass `against` when a handoff names the SHA it landed as. Comparing to a
    moved `main` gives a false negative: work really was landed content-identical
    at commit X, and files it touched changed again in commits after X.
    """
    target = against or main_ref
    if not commit_exists(repo, sha):
        return {"checked": False, "reason": "commit not present"}
    if is_ancestor(repo, sha, main_ref):
        return {"checked": True, "landed_by_sha": True, "landed_by_content": True,
                "compared_against": main_ref}
    base = merge_base(repo, sha, main_ref)
    paths = changed_paths(repo, sha, base)
    if not paths:
        return {"checked": True, "landed_by_sha": False, "landed_by_content": None,
                "reason": "branch changed no files against its merge base"}
    value, error = _git(repo, "diff", "--name-only", sha, target, "--", *paths)
    if error is not None:
        return {"checked": False, "reason": error}
    differing = [line for line in (value or "").splitlines() if line]
    return {"checked": True, "landed_by_sha": False,
            "landed_by_content": not differing, "compared_against": target,
            "paths_touched": len(paths), "paths_still_differing": differing}


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
