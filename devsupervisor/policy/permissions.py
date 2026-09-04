"""Execution policy: how much autonomy a worker process gets.

Permission bypass is *autonomy inside a disposable sandbox*, never *authority*.
A worker with bypass can run whatever it needs to do its job without stopping to
ask. It still cannot land, push, rewrite history, mutate a benchmark, or spend
outside budget — those are decided by DevSupervisor before and after the worker
runs, and the worker's tool permissions have no bearing on them.

The distinction matters because the two are easy to conflate: a process that can
do anything in its sandbox looks, from inside, exactly like a process that has
been authorised to do anything.
"""

from ..errors import PolicyViolation

# Documented CLI vocabulary, verified against the installed binary's own
# --permission-mode choice list.
BYPASS = "bypassPermissions"
GUARDED = "dontAsk"
ACCEPT_EDITS = "acceptEdits"
PLAN = "plan"
MODES = (BYPASS, GUARDED, ACCEPT_EDITS, "auto", "manual", PLAN)

# Disposable roles: they work in their own worktree, produce an artifact, and are
# thrown away. Nothing they do reaches shared state except through a later,
# separately authorised step.
ISOLATED_BYPASS_ROLES = frozenset({
    "build", "reviewer", "specialist", "security", "researcher", "investigator",
    "benchmark", "evaluator", "qa", "architect",
})

# Privileged roles act on shared state. They are bounded by a deterministic
# contract, not by an LLM's judgment, so they never get bypass.
PRIVILEGED_ROLES = frozenset({"landing", "operator", "freeze", "supervisor", "planner"})

POLICY_NAME = "execution.permission_mode"
POLICY_KIND = "session_reuse"   # closest existing learnable category

DEFAULT_POLICY = {
    "default": GUARDED,
    "roles": {role: BYPASS for role in sorted(ISOLATED_BYPASS_ROLES)},
    "rationale": (
        "Disposable workers run unattended in their own worktree, so a permission "
        "prompt nobody is there to answer is just a failed run. Privileged roles "
        "stay guarded because their limits are a contract, not a preference."
    ),
}


def mode_for(role, policy=None):
    """The permission mode a role runs under."""
    policy = policy or DEFAULT_POLICY
    mode = (policy.get("roles") or {}).get(role, policy.get("default", GUARDED))
    if mode not in MODES:
        raise PolicyViolation(f"unknown permission mode {mode!r}; expected one of {MODES}")
    return mode


def is_bypass(mode):
    return mode == BYPASS


def requires_isolation(mode):
    """Bypass is only acceptable in a checkout that is not shared."""
    return is_bypass(mode)


def isolation_problem(worktree, shared_paths=()):
    """Why this location is not safe to run a bypassed worker in, or None."""
    if not worktree:
        return "no isolated worktree is configured for this job"
    resolved = str(worktree).rstrip("/")
    for shared in shared_paths:
        if shared and resolved == str(shared).rstrip("/"):
            return f"{resolved} is a shared checkout, not a disposable worktree"
    return None


def check(role, mode, worktree=None, shared_paths=()):
    """Assert a permission mode is legitimate. Raises rather than adjusting."""
    if mode not in MODES:
        raise PolicyViolation(f"unknown permission mode {mode!r}")
    if not is_bypass(mode):
        return True
    if role in PRIVILEGED_ROLES:
        raise PolicyViolation(
            f"role {role!r} acts on shared state and must not run with permission "
            f"bypass; its limits are a deterministic contract, not a tool policy"
        )
    problem = isolation_problem(worktree, shared_paths)
    if problem:
        raise PolicyViolation(
            f"role {role!r} cannot run bypassed: {problem}")
    return True


def resolve(role, worktree=None, shared_paths=(), policy=None):
    """The mode a job will actually run under, and why if it is not the policy one.

    A privileged role asking for bypass is a policy error and raises. Missing
    isolation is an environment fact, not a mistake, so it downgrades to guarded
    instead — but never silently: the caller records the reason on the run.
    """
    mode = mode_for(role, policy)
    if not is_bypass(mode):
        return mode, None
    if role in PRIVILEGED_ROLES:
        check(role, mode)          # raises with the right message
    problem = isolation_problem(worktree, shared_paths)
    if problem:
        return GUARDED, f"bypass withheld: {problem}"
    return mode, None


def describe(policy=None):
    policy = policy or DEFAULT_POLICY
    rows = []
    for role in sorted(set(ISOLATED_BYPASS_ROLES | PRIVILEGED_ROLES)):
        mode = mode_for(role, policy)
        marker = "  [privileged]" if role in PRIVILEGED_ROLES else ""
        rows.append(f"  {role:13} -> {mode}{marker}")
    return "\n".join(rows)
