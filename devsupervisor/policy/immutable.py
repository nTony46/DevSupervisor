"""Immutable safety invariants.

These are constants in source, not rows in a database, and this module exposes
no mutation API. That absence is the enforcement: a retrospective, a worker, or
an LLM has nothing to call. Learnable orchestration policy lives in
`policy.learnable`, which is versioned and reviewable.
"""

from types import MappingProxyType

from ..errors import HumanGateRequired, PolicyViolation

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
    "Never route a critical reasoning role below the strongest available Opus "
    "model at high effort, and never give one a fallback model. Downgrading "
    "judgment is a silent change to what review means.",
    "Both arms of a paired experiment run identically except for the treatment "
    "under test. A difference in model, effort, budget, timeout, tools, base "
    "commit, or task body makes the comparison meaningless.",
    "Only the supervisor creates jobs. A worker may request a subtask; it may "
    "not spawn one.",
    "A CRITICAL job does not run until a human gate for it is recorded as "
    "approved. Planning is not the only way a job is created, so this is "
    "enforced where every job passes: dispatch.",
    "Permission bypass is autonomy inside a disposable sandbox, never authority. "
    "It grants no right to land, push, rewrite history, mutate a benchmark, or "
    "spend outside budget; those are decided before and after the worker runs.",
    "Landing is bounded by a deterministic contract — an approved exact SHA, a "
    "target resolved at landing time, and a verified fast-forward — not by what "
    "tools the landing worker happens to hold.",
)

# Roles whose judgment the rest of the system is calibrated against. A cheaper
# model here does not save money, it changes what "approved" means.
CRITICAL_ROLES = frozenset({
    "supervisor", "reviewer", "specialist", "security", "evaluator", "benchmark",
})

# The floor for those roles. Stronger is allowed; weaker is not.
MODEL_TIERS = ("haiku", "sonnet", "opus")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
ROUTING_FLOOR = MappingProxyType({"tier": "opus", "effort": "high"})

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


def _rank(value, scale, default=-1):
    return scale.index(value) if value in scale else default


def check_routing(role, tier, effort, fallback_model=None):
    """Refuse a routing decision that puts a critical role below the floor."""
    if role not in CRITICAL_ROLES:
        return True
    floor_tier, floor_effort = ROUTING_FLOOR["tier"], ROUTING_FLOOR["effort"]
    if _rank(tier, MODEL_TIERS) < _rank(floor_tier, MODEL_TIERS):
        raise PolicyViolation(
            f"role {role!r} is critical and cannot be routed to tier {tier!r}; "
            f"the floor is {floor_tier!r}"
        )
    if _rank(effort, EFFORT_LEVELS) < _rank(floor_effort, EFFORT_LEVELS):
        raise PolicyViolation(
            f"role {role!r} is critical and cannot run at effort {effort!r}; "
            f"the floor is {floor_effort!r}"
        )
    if fallback_model:
        raise PolicyViolation(
            f"role {role!r} is critical and must not be given a fallback model "
            f"({fallback_model!r}): a fallback is a downgrade nobody chose"
        )
    return True


def check_routing_policy(body):
    """Refuse a *proposed* routing policy that would downgrade a critical role.

    This is what stops a retrospective from quietly making review cheaper. It
    runs at proposal time, so the candidate never reaches a reviewer looking
    legitimate.
    """
    if not isinstance(body, dict):
        return True
    default = body.get("default") or {}
    for role, entry in (body.get("roles") or {}).items():
        if role not in CRITICAL_ROLES:
            continue
        merged = {**default, **(entry or {})}
        check_routing(role, merged.get("tier", ROUTING_FLOOR["tier"]),
                      merged.get("effort", ROUTING_FLOOR["effort"]),
                      merged.get("fallback_model"))
    if default:
        for role in sorted(CRITICAL_ROLES):
            if role in (body.get("roles") or {}):
                continue
            check_routing(role, default.get("tier", ROUTING_FLOOR["tier"]),
                          default.get("effort", ROUTING_FLOOR["effort"]),
                          default.get("fallback_model"))
    return True


def check_critical_gate(job, approved_gates):
    """Refuse to dispatch CRITICAL work with no recorded approval.

    The planner opens gates for CRITICAL plans, but a job can also be created
    directly, and that path had no gate at all. Enforcing here rather than in the
    planner means the guarantee holds for every way a job comes into existence.
    """
    if job.get("risk") != "CRITICAL":
        return True
    if not approved_gates:
        raise HumanGateRequired(
            f"job {job['id']} is CRITICAL and has no approved human gate; "
            f"it will not be dispatched"
        )
    return True


def check_approval(job, approvals):
    """Refuse to land review-requiring work without a recorded approval."""
    if job["review_policy"] != "none" and not approvals:
        raise PolicyViolation(
            f"job {job['id']}: review_policy={job['review_policy']!r} but no approval is recorded"
        )
    return True
