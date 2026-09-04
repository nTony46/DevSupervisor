"""Model routing.

Which model and how much thinking each role gets is *policy*, not orchestration
logic: the scheduler asks the router and does what it is told. The router in
turn asks discovery what this machine actually supports, so no marketing string
is hardcoded anywhere.

The opening policy is deliberately expensive. Every reasoning role, mechanical
ones included, runs on the strongest available Opus at high effort, so the first
real campaigns produce a quality baseline that later cost-cutting has to be
measured against. Cheaper routing can only arrive as a retrospective candidate
with observed outcomes behind it — and never for a critical role.
"""

from dataclasses import asdict, dataclass

from ..providers import discovery
from . import immutable, learnable

POLICY_NAME = "model_routing.default"
POLICY_KIND = "model_choice"

# Every role the supervisor can dispatch. Listed explicitly so a new role cannot
# be routed by accident: an unknown role falls back to the default entry, which
# is the strongest setting, not the cheapest.
ROUTED_ROLES = (
    "supervisor", "planner", "architect", "build", "reviewer", "specialist",
    "security", "benchmark", "evaluator", "researcher", "investigator", "qa",
    "landing", "freeze", "operator",
)

# Roles whose work is mechanical rather than judgment-heavy. They are routed to
# Opus/high anyway for now; the distinction exists so a retrospective has a
# category to propose against.
MECHANICAL_ROLES = frozenset({"landing", "freeze", "qa", "operator"})

DEFAULT_POLICY = {
    "default": {"tier": "opus", "effort": "high", "fallback_model": None},
    "roles": {},
    "rationale": (
        "Opening baseline: correctness and orchestration quality over cost. "
        "Cheaper routing must be earned through observed outcomes."
    ),
}


@dataclass(frozen=True)
class RoutingDecision:
    role: str
    tier: str
    model_id: str
    model_alias: str
    model_concrete: bool
    effort: str
    fallback_model: str
    provider: str
    source: str
    resolution_source: str
    critical: bool
    max_budget_usd: float = None

    def to_dict(self):
        return asdict(self)

    def describe(self):
        return (f"{self.role:12} -> {self.model_id} @ effort {self.effort}"
                f"{'  [critical]' if self.critical else ''}")


class ModelRouter:
    """Resolves a role to a concrete model and effort, then checks the floor."""

    def __init__(self, store=None, resolution=None, provider_name="claude-cli",
                 policy=None, max_budget_usd=None):
        self.store = store
        self.provider_name = provider_name
        self.max_budget_usd = max_budget_usd
        self._resolution = resolution
        self._policy_override = policy

    @property
    def resolution(self):
        if self._resolution is None:
            floor = immutable.ROUTING_FLOOR["effort"]
            self._resolution = discovery.resolve_opus(minimum_effort=floor)
        return self._resolution

    def policy(self):
        if self._policy_override is not None:
            return self._policy_override
        if self.store is None:
            return DEFAULT_POLICY
        return learnable.effective(self.store, POLICY_NAME, DEFAULT_POLICY)

    def route(self, role):
        policy = self.policy()
        default = policy.get("default") or DEFAULT_POLICY["default"]
        entry = (policy.get("roles") or {}).get(role) or {}
        merged = {**default, **entry}
        source = "adopted policy" if entry else "policy default"
        if self.store is not None and self._policy_override is None:
            if learnable.active(self.store, POLICY_NAME) is None:
                source = f"built-in default ({source})"

        tier = merged.get("tier", "opus")
        effort = merged.get("effort", immutable.ROUTING_FLOOR["effort"])
        fallback = merged.get("fallback_model")
        critical = role in immutable.CRITICAL_ROLES

        # The floor is checked at the point of use, not only at proposal time,
        # so a policy adopted by any path still cannot downgrade judgment.
        immutable.check_routing(role, tier, effort, fallback)

        resolution = self.resolution
        model_id = merged.get("model_id") or (
            resolution.model_id if tier == "opus" else tier)
        return RoutingDecision(
            role=role, tier=tier, model_id=model_id, model_alias=resolution.alias,
            model_concrete=resolution.concrete and tier == "opus",
            effort=effort, fallback_model=fallback, provider=self.provider_name,
            source=source, resolution_source=resolution.model_source, critical=critical,
            max_budget_usd=merged.get("max_budget_usd", self.max_budget_usd),
        )

    def table(self):
        return [self.route(role) for role in ROUTED_ROLES]

    def render(self):
        resolution = self.resolution
        lines = [
            f"resolved model : {resolution.describe()}",
            f"supported effort levels: {', '.join(resolution.supported_efforts)}",
            f"critical roles : {', '.join(sorted(immutable.CRITICAL_ROLES))}",
            f"floor          : tier {immutable.ROUTING_FLOOR['tier']}, "
            f"effort {immutable.ROUTING_FLOOR['effort']} (immutable)",
            "",
        ]
        for decision in self.table():
            lines.append(f"  {decision.describe()}")
        for note in resolution.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)
