"""Workflow templates.

A template is a small ordered list of steps with dependencies — enough shape to
be useful, not so much that it dictates how a competent worker does the job.
The planner may drop steps the risk level does not warrant.
"""

from dataclasses import dataclass, field

from ..policy import risk as risk_module

FEATURE = "feature"
BUG = "bug"
REFACTOR = "refactor"
RESEARCH = "research"
EXPERIMENT = "experiment"
MIGRATION = "migration"


@dataclass(frozen=True)
class Step:
    key: str
    role: str
    title: str
    scope: str
    non_goals: str = ""
    output_contract: str = ""
    depends: tuple = ()
    # Included only when the goal's risk is at least this. LOW keeps it always.
    min_risk: str = risk_module.LOW
    # None means "derive from risk"; a literal wins (a reviewer is not reviewed).
    review_policy: str = None
    acceptance: tuple = field(default_factory=tuple)


_REVIEW_STEP = Step(
    key="review", role="reviewer", title="Independent review",
    scope="Review the candidate against its contract. Return APPROVE, or REJECT "
          "with numbered blockers.",
    non_goals="Do not fix the code you are reviewing. Do not review taste.",
    output_contract="verdict APPROVE|REJECT, blockers[] when REJECT, evidence[]",
    depends=("build",), min_risk=risk_module.MEDIUM, review_policy="none",
)

_SPECIALIST_STEP = Step(
    key="specialist", role="specialist", title="Specialist review",
    scope="Review the candidate for the risk this change carries "
          "(security, performance, or QA as assigned).",
    non_goals="Do not repeat the general code review. Do not fix the code.",
    output_contract="verdict APPROVE|REJECT, blockers[] when REJECT, evidence[]",
    depends=("build",), min_risk=risk_module.HIGH, review_policy="none",
)

def _land(*depends):
    """Landing waits on every gate the template actually has."""
    return Step(
        key="land", role="landing", title="Land the approved candidate",
        scope="Verify the approved SHA still exists and matches what was reviewed, "
              "detect target-branch movement, land with safe git operations, run the "
              "project's required verification, record the final target SHA.",
        non_goals="Never force push. Never rewrite history. Invent no unrelated fixes.",
        output_contract="landing report: candidate sha, base at review, final target sha, "
                        "verification output",
        depends=depends, review_policy="none",
    )

_EVALUATE_STEP = Step(
    key="evaluate", role="evaluator", title="Goal completion check",
    scope="Ask whether the landed change actually satisfies the goal's acceptance "
          "criteria. This is not a code review.",
    non_goals="Do not re-review implementation quality.",
    output_contract="verdict APPROVE|REJECT against the goal criteria, evidence[]",
    depends=("land",), review_policy="none",
)


TEMPLATES = {
    FEATURE: (
        Step("inspect", "investigator", "Inspect the current system",
             "Read the code that this feature touches and report how it works today.",
             non_goals="Change nothing.",
             output_contract="report artifact describing current behavior and the seams",
             min_risk=risk_module.MEDIUM, review_policy="none"),
        Step("contract", "planner", "Define the acceptance contract",
             "State what will be built, what is out of scope, what done means, and how "
             "it will be verified.",
             non_goals="Do not specify implementation details a builder can safely choose.",
             output_contract="job contract artifact", depends=("inspect",),
             min_risk=risk_module.MEDIUM, review_policy="none"),
        Step("design", "architect", "Design the change",
             "Produce a design for the change, including the alternatives rejected.",
             output_contract="design artifact", depends=("contract",),
             min_risk=risk_module.HIGH, review_policy="none"),
        Step("build", "build", "Implement the feature",
             "Implement the feature against the contract and verify it with real output.",
             non_goals="Nothing outside the contract's scope.",
             output_contract="commit sha, diff artifact, test log artifact",
             depends=("contract", "design")),
        _REVIEW_STEP, _SPECIALIST_STEP, _land("review", "specialist"), _EVALUATE_STEP,
    ),
    BUG: (
        Step("reproduce", "investigator", "Reproduce the defect",
             "Reproduce the reported behavior and capture the exact evidence.",
             non_goals="Do not fix anything yet.",
             output_contract="reproduction artifact with commands and observed output",
             min_risk=risk_module.MEDIUM, review_policy="none"),
        Step("diagnose", "investigator", "Diagnose the root cause",
             "Explain why the defect happens, citing the code path.",
             output_contract="diagnosis artifact", depends=("reproduce",),
             min_risk=risk_module.MEDIUM, review_policy="none"),
        Step("contract", "planner", "Define the regression condition",
             "State the condition that must hold forever after this fix.",
             output_contract="regression condition artifact", depends=("diagnose",),
             min_risk=risk_module.MEDIUM, review_policy="none"),
        Step("build", "build", "Fix the defect",
             "Fix the root cause and add the regression test that fails without the fix.",
             non_goals="No refactoring beyond the fix.",
             output_contract="commit sha, diff artifact, failing-then-passing test evidence",
             depends=("contract",)),
        _REVIEW_STEP, _SPECIALIST_STEP, _land("review", "specialist"), _EVALUATE_STEP,
    ),
    REFACTOR: (
        Step("baseline", "investigator", "Capture baseline behavior",
             "Record the behavior and tests that must be unchanged afterwards.",
             output_contract="baseline artifact including test output",
             min_risk=risk_module.MEDIUM, review_policy="none"),
        Step("contract", "planner", "Define the refactor boundaries",
             "State exactly which modules move and which behavior is frozen.",
             output_contract="job contract artifact", depends=("baseline",),
             min_risk=risk_module.MEDIUM, review_policy="none"),
        Step("build", "build", "Perform the refactor",
             "Restructure within the boundaries without changing behavior.",
             non_goals="No behavior changes, no new features.",
             output_contract="commit sha, diff artifact", depends=("contract",)),
        Step("equivalence", "qa", "Verify behavior equivalence",
             "Prove behavior is unchanged against the captured baseline.",
             output_contract="verdict APPROVE|REJECT with the comparison evidence",
             depends=("build",), review_policy="none"),
        _REVIEW_STEP, _land("review", "equivalence"), _EVALUATE_STEP,
    ),
    RESEARCH: (
        Step("question", "planner", "Sharpen the question",
             "State the decision this research must inform and what would change it.",
             output_contract="question artifact", review_policy="none"),
        Step("research", "researcher", "Research the question",
             "Gather evidence and summarize the options with their tradeoffs.",
             output_contract="research artifact citing sources", depends=("question",),
             review_policy="none"),
        Step("critique", "reviewer", "Adversarial critique",
             "Attack the research: what is unsupported, what was not considered.",
             non_goals="Do not rewrite the research.",
             output_contract="verdict APPROVE|REJECT with specific weaknesses",
             depends=("research",), review_policy="none"),
        Step("recommend", "planner", "Write the decision artifact",
             "Recommend one option and record the rationale as a decision record.",
             output_contract="decision artifact", depends=("critique",), review_policy="none"),
    ),
    EXPERIMENT: (
        Step("hypothesis", "planner", "State the hypothesis",
             "State the hypothesis and the metric that would falsify it.",
             output_contract="hypothesis artifact", review_policy="none"),
        Step("design", "architect", "Design the experiment",
             "Design the experiment, including controls and the stopping rule.",
             output_contract="experiment design artifact", depends=("hypothesis",),
             review_policy="none"),
        Step("integrity", "reviewer", "Integrity review",
             "Check the design for leakage, overfitting, and unfalsifiable claims.",
             non_goals="Do not redesign the experiment.",
             output_contract="verdict APPROVE|REJECT with specific integrity risks",
             depends=("design",), review_policy="none"),
        Step("build", "build", "Implement the experiment",
             "Implement the experiment exactly as designed.",
             output_contract="commit sha, runnable harness artifact", depends=("integrity",)),
        Step("run", "operator", "Run the experiment",
             "Execute the experiment and capture the raw results.",
             output_contract="raw results artifact", depends=("build",), review_policy="none"),
        Step("analyse", "researcher", "Analyse the results",
             "Analyse the results against the stated metric, including negative results.",
             output_contract="analysis artifact", depends=("run",), review_policy="none"),
        Step("decide", "planner", "Record the decision",
             "Record what the experiment decided, or that it decided nothing.",
             output_contract="decision artifact", depends=("analyse",), review_policy="none"),
    ),
    MIGRATION: (
        Step("inspect", "investigator", "Inspect the current state",
             "Record the exact current schema, data shape, and callers.",
             output_contract="current-state artifact", review_policy="none"),
        Step("contract", "planner", "Write the migration plan",
             "Plan the migration in reversible stages with an explicit rollback.",
             output_contract="migration plan artifact", depends=("inspect",),
             review_policy="none"),
        Step("reversibility", "reviewer", "Reversibility review",
             "Check that every stage can be rolled back and that data cannot be lost.",
             non_goals="Do not rewrite the plan.",
             output_contract="verdict APPROVE|REJECT with the irreversible steps named",
             depends=("contract",), review_policy="none"),
        Step("build", "build", "Implement the migration",
             "Implement the migration and its rollback path.",
             output_contract="commit sha, diff artifact, dry-run output",
             depends=("reversibility",)),
        Step("validate", "qa", "Validate against real data shape",
             "Validate the migration on a copy, including the rollback.",
             output_contract="validation artifact", depends=("build",), review_policy="none"),
        _REVIEW_STEP, _land("review", "validate"), _EVALUATE_STEP,
    ),
}


def template(workflow):
    if workflow not in TEMPLATES:
        raise ValueError(f"unknown workflow {workflow!r}; expected one of {sorted(TEMPLATES)}")
    return TEMPLATES[workflow]


def steps_for(workflow, level):
    """The steps this risk level warrants, with dangling dependencies rewired.

    Dropping a step must not orphan the steps that depended on it, so a dropped
    step's dependencies are inherited by whoever depended on it.
    """
    kept, dropped = [], {}
    for step in template(workflow):
        if risk_module.at_least(level, step.min_risk):
            kept.append(step)
        else:
            dropped[step.key] = step.depends

    surviving = {step.key for step in kept}
    resolved = []
    for step in kept:
        depends = []
        for key in step.depends:
            depends.extend(_resolve(key, dropped, set()))
        ordered = [key for key in dict.fromkeys(depends) if key in surviving]
        resolved.append((step, tuple(ordered)))
    return resolved


def _resolve(key, dropped, seen):
    if key not in dropped:
        return [key]
    if key in seen:  # pragma: no cover - templates are acyclic
        return []
    seen.add(key)
    out = []
    for parent in dropped[key]:
        out.extend(_resolve(parent, dropped, seen))
    return out


def available():
    return sorted(TEMPLATES)
