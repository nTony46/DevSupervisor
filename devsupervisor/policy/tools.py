"""Tool profiles per role.

A reviewer that can edit is not a reviewer. A landing job that can force push is
a liability. So what a worker may do is decided by its role, here, in policy —
not by whatever the runtime happens to default to.

These are allowlists: anything not named is denied by the runtime, which is the
right direction for a default.
"""

# Reading the repository and asking git questions about it. No writes anywhere.
INSPECT = (
    "Read", "Grep", "Glob",
    "Bash(git diff:*)", "Bash(git show:*)", "Bash(git log:*)", "Bash(git status:*)",
    "Bash(git rev-parse:*)", "Bash(git merge-base:*)", "Bash(git branch:*)",
    "Bash(git ls-files:*)", "Bash(git cat-file:*)", "Bash(git worktree list:*)",
    "Bash(ls:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(wc:*)", "Bash(head:*)",
    "Bash(tail:*)", "Bash(cat:*)",
)

# Running the project's own checks. Still no source edits.
VERIFY = INSPECT + (
    "Bash(cargo test:*)", "Bash(cargo check:*)", "Bash(cargo clippy:*)",
    "Bash(cargo fmt:*)", "Bash(cargo build:*)", "Bash(cargo deny:*)",
    "Bash(./scripts/verify.sh:*)", "Bash(python3 -m unittest:*)",
    "Bash(python3 -m pytest:*)", "Bash(npm test:*)", "Bash(node --test:*)",
)

# Producing a change.
IMPLEMENT = VERIFY + ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash(git add:*)")

# Landing. Explicitly enumerated, and force-push shapes are simply not present.
LAND = VERIFY + (
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git merge --ff-only:*)",
    "Bash(git switch:*)", "Bash(git checkout:*)", "Bash(git cherry-pick:*)",
    "Bash(git push origin:*)", "Bash(git fetch:*)", "Bash(git tag:*)",
)

# Freezing records an approved SHA as authoritative. It runs the artifact's own
# checks and writes nothing to the repository at all — that is the difference
# between freezing and landing.
FREEZE = VERIFY

ROLE_PROFILES = {
    "freeze": FREEZE,
    "reviewer": VERIFY,
    "specialist": VERIFY,
    "security": VERIFY,
    "evaluator": VERIFY,
    "qa": VERIFY,
    "investigator": INSPECT,
    "researcher": INSPECT,
    "architect": INSPECT,
    "planner": INSPECT,
    "supervisor": INSPECT,
    "benchmark": INSPECT,
    "build": IMPLEMENT,
    "operator": VERIFY,
    "landing": LAND,
}

# Roles that must not be able to change the tree they are looking at.
READ_ONLY_ROLES = frozenset({
    "reviewer", "specialist", "security", "evaluator", "qa", "investigator",
    "researcher", "architect", "planner", "supervisor", "benchmark", "freeze",
})

_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "git commit", "git push")


def profile_for(role):
    """The allowlist for a role. Unknown roles get the most restrictive profile."""
    return ROLE_PROFILES.get(role, INSPECT)


def is_read_only(role):
    return role in READ_ONLY_ROLES


def assert_read_only(role, tools):
    """Guard: a read-only role must never be handed a writing tool."""
    if not is_read_only(role):
        return True
    offending = [tool for tool in tools
                 if any(write in tool for write in _WRITE_TOOLS)]
    if offending:
        from ..errors import PolicyViolation
        raise PolicyViolation(
            f"role {role!r} is read-only but was given writing tools: {offending}")
    return True
