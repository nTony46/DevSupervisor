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

# Running the project's own checks.
#
# This one is deliberately broad rather than a list of command prefixes. A
# prefix entry cannot match `PYTHONPATH=. python3 -m unittest` — a real command
# a real reviewer needed — and a reviewer that hits one denial tends to conclude
# it has no shell at all and withhold its verdict. Enumerating every safe
# invocation is not achievable; verifying the tree did not change is. The
# guarantee for these roles comes from `integrity.assert_unchanged`, not from
# hoping this list is complete.
VERIFY = ("Read", "Grep", "Glob", "Bash")

# Never available to a read-only role, whatever else is allowed.
DISALLOWED_FOR_READ_ONLY = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Producing a change.
IMPLEMENT = VERIFY + ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Landing needs to write to the repository, so it gets a shell. What it must not
# do is enforced by the immutable command check, which refuses force-push and
# history-rewrite shapes outright.
LAND = VERIFY

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

_WRITE_TOOLS = DISALLOWED_FOR_READ_ONLY


def profile_for(role):
    """The allowlist for a role. Unknown roles get the most restrictive profile."""
    return ROLE_PROFILES.get(role, INSPECT)


def disallowed_for(role):
    """Tools denied outright, regardless of the allowlist."""
    return DISALLOWED_FOR_READ_ONLY if is_read_only(role) else ()


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
