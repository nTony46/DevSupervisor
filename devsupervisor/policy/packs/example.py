"""Example project policy pack.

A worked example of everything a pack can say about a project, for a made-up
repository: a workspace with product crates, a measurement harness, and a
frozen benchmark that implementers must never see the answers to. Copy this
file to `$DEVSUPERVISOR_HOME/packs/<project>.py`, rename the class and `name`,
and edit the rules — the pack for a real project lives there, not in this
repository. A pack may only add restrictions; it cannot relax an immutable rule.
"""

from ...errors import PolicyViolation
from ...policy import risk as risk_module
from . import register
from .base import PolicyPack


@register
class ExamplePack(PolicyPack):
    name = "example"
    description = (
        "A workspace with product crates, a measurement harness, and a frozen "
        "downstream benchmark. Review is independent and by exact SHA; landing is "
        "a separate lane; benchmark packages are frozen."
    )

    extra_rules = (
        "Review and land by exact SHA. A branch name is not a candidate: branches "
        "move between review and landing.",
        "Land with fast-forward or a clean merge only. Never force push, never "
        "rebase a reviewed SHA, never rewrite a pushed branch.",
        "Frozen benchmark packages are immutable. Reading them is fine; changing "
        "one needs a human gate.",
        "Benchmark oracle data and held-out pairs never reach an implementation "
        "worker. Benchmark authoring is clean-room: it must not read the fixes "
        "it will later measure.",
        "A harness commit is not a product commit. scripts/harness/ is "
        "measurement; crates/, apps/, migrations/ and Cargo.* are the product. "
        "Never record one as the other.",
        "Benchmark claims are correctness-first. A tie or a control win is a "
        "result and is reported as one; never tune the product to move a benchmark.",
        "Paid A/B or agent-swarm runs need a configured budget and an explicit "
        "human approval before they start.",
        "The implementer never lands its own work, and never reviews it.",
        "verify.sh is the proof. A lane is not done because it looks done.",
    )

    protected_paths = (
        "benchmarks/frozen/candidate-", "benchmarks/frozen/pair-",
    )

    required_verification = ("./scripts/verify.sh",)

    # Workers building product code must never be handed benchmark answers.
    clean_room_roles = ("build", "architect", "investigator", "planner")
    clean_room_paths = (
        "benchmarks/frozen/", "oracle", "held-out", "/expected/", "reference-patch",
    )

    gate_triggers = (
        ("benchmark_mutation", ("frozen benchmark", "benchmark baseline",
                                "candidate package", "reseal", "re-freeze")),
        ("budget", ("paid a/b", "a/b run", "paid experiment", "agent swarm",
                    "launch an a/b")),
        ("destructive", ("force push", "rewrite history", "delete branch",
                         "erase all", "reset --hard")),
        ("privacy", ("telemetry", "upload", "send to server", "cloud sync")),
    )

    # Decisions carried over from before the supervisor existed. Each one is
    # here because inspection cannot settle it, which is the test for whether
    # something belongs in front of a person rather than in front of a worker.
    # They become human gates when the project's handoffs are imported.
    open_decisions = (
        ("budget",
         "Approve the paid benchmark A/B run?",
         "Instrumentation is built and accepted; the run has not started. Prior "
         "runs show ties and one control win — no treatment win yet."),
        ("benchmark_mutation",
         "Approve a second source repository for benchmark pairs 03 and 04?",
         "Its test suite was proven to run. The first source's high-quality pair "
         "surface is exhausted; pairs 03/04 are blocked on this."),
        ("conflicting_evidence",
         "Three prior landings were gated on 'reviewer approved' preconditions "
         "confirmed only in conversation. Re-verify or accept?",
         "Those gates left no artifact. This harness records gates as rows so the "
         "situation does not recur, but the historical landings remain unverified."),
    )

    # What each part of the tree is, for commit-identity checks.
    PRODUCT_PREFIXES = ("crates/", "apps/", "migrations/", "Cargo.toml", "Cargo.lock")
    HARNESS_PREFIXES = ("scripts/harness/", "scripts/verify.sh", "docs/")

    def risk_override(self, text="", paths=(), risk=None):
        """Subject matter that is riskier in this project than its words suggest."""
        haystack = " ".join([text or ""] + [str(p) for p in paths]).lower()
        if any(word in haystack for word in
               ("frozen benchmark", "benchmark baseline", "candidate package",
                "oracle", "held-out")):
            return risk_module.CRITICAL
        if any(word in haystack for word in
               ("retrieval", "ranking", "benchmark", "harness", "provenance")):
            return risk_module.HIGH
        return None

    # --- commit identity --------------------------------------------------

    def commit_identity(self, paths):
        """'product', 'harness', 'mixed', or 'unknown' for a set of changed paths."""
        product = any(str(p).startswith(self.PRODUCT_PREFIXES) for p in paths)
        harness = any(str(p).startswith(self.HARNESS_PREFIXES) for p in paths)
        if product and harness:
            return "mixed"
        if product:
            return "product"
        if harness:
            return "harness"
        return "unknown"

    def check_product_sha(self, sha, paths):
        """Refuse to record a harness-only commit as the product SHA under test."""
        identity = self.commit_identity(paths)
        if identity == "harness":
            raise PolicyViolation(
                f"{sha[:12]} touches only measurement code "
                f"({', '.join(self.HARNESS_PREFIXES)}); it is not a product SHA"
            )
        return identity
