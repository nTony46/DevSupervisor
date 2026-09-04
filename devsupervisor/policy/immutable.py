"""Immutable safety invariants.

These are constants in source, not rows in a database, and this module exposes
no mutation API. That absence is the enforcement: a retrospective, a worker, or
an LLM has nothing to call. Learnable orchestration policy lives in
`policy.learnable`, which is versioned and reviewable.
"""

from types import MappingProxyType

from ..errors import PolicyViolation

RULES = (
    "Never force push; never rewrite shared history destructively.",
    "Never silently mutate a frozen benchmark or evaluation package.",
    "Never leak private oracle or held-out data into an implementation worker.",
    "Never store .env contents, credentials, tokens, private keys, or secrets in "
    "shared memory or a job packet.",
    "Never use an eval or harness commit as if it were a product SHA.",
    "Never land code that requires review without a recorded approval.",
    "A builder may never be the independent reviewer of its own work.",
    "Never start substantial paid model or agent runs without a configured budget "
    "and an approval.",
    "Stop on ambiguous repository identity rather than guessing.",
    "Require explicit approval for irreversible or destructive actions.",
)

# Command fragments that are refused outright, whatever a plan or a model says.
FORBIDDEN_COMMANDS = MappingProxyType({
    "push --force": "force push",
    "push -f": "force push",
    "push --force-with-lease": "force push",
    "reset --hard": "destructive reset of a shared checkout",
    "clean -fdx": "destructive clean",
    "filter-branch": "history rewrite",
    "rebase --root": "history rewrite",
    "branch -D": "branch deletion",
})


def rules():
    """The rules, as an immutable tuple. There is deliberately no setter."""
    return RULES


def render():
    return "\n".join(f"{index}. {rule}" for index, rule in enumerate(RULES, start=1))


def check_command(command):
    """Refuse a shell command that would break an immutable rule."""
    lowered = " ".join((command or "").split()).lower()
    for fragment, description in FORBIDDEN_COMMANDS.items():
        if fragment in lowered:
            raise PolicyViolation(f"refused: {description} ({fragment!r} in command)")
    return True


def check_approval(job, approvals):
    """Refuse to land review-requiring work without a recorded approval."""
    if job["review_policy"] != "none" and not approvals:
        raise PolicyViolation(
            f"job {job['id']}: review_policy={job['review_policy']!r} but no approval is recorded"
        )
    return True
