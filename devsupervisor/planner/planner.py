"""Goal -> plan DAG.

The planner is deterministic: given a goal, a workflow, and a risk level it
produces the same graph every time. An LLM may choose the workflow or sharpen
the scope text, but it does not decide what review a risk level requires.
"""

from .. import gates
from ..policy import risk as risk_module
from ..policy.packs import PolicyPack
from ..state import machine
from . import workflows

# A reviewer cannot wait for the thing it reviews to finish, so its dependency
# edge is satisfied earlier than DONE.
_EDGE_THRESHOLDS = {
    ("review", "build"): machine.UNDER_REVIEW,
    ("specialist", "build"): machine.UNDER_REVIEW,
    ("equivalence", "build"): machine.WORK_COMPLETE,
    ("validate", "build"): machine.WORK_COMPLETE,
    ("land", "build"): machine.LANDING_READY,
}

# Steps whose job acts on the build job rather than producing its own candidate.
_REVIEWS_BUILD = ("review", "specialist", "equivalence", "validate", "evaluate")


_WORKFLOW_HINTS = (
    (workflows.BUG, ("bug", "fix", "defect", "regression", "broken", "crash", "fails")),
    (workflows.REFACTOR, ("refactor", "restructure", "clean up", "extract", "rename")),
    (workflows.MIGRATION, ("migrat", "upgrade", "port to", "move storage", "backfill")),
    (workflows.EXPERIMENT, ("experiment", "a/b", "measure whether", "benchmark", "pilot")),
    (workflows.RESEARCH, ("research", "investigate", "evaluate options", "recommend",
                          "compare", "decide whether")),
    (workflows.FEATURE, ("add", "build", "implement", "support", "introduce")),
)


def infer_workflow(text):
    """Pick a template from the goal's own words. Feature is the safe default."""
    lowered = (text or "").lower()
    for workflow, needles in _WORKFLOW_HINTS:
        if any(needle in lowered for needle in needles):
            return workflow
    return workflows.FEATURE


class Planner:
    def __init__(self, store, pack=None):
        self.store = store
        self.pack = pack or PolicyPack()

    def plan(self, goal, workflow=None, risk_level=None, subject=None,
             repo=None, branch=None, base_sha=None, worktree=None, rationale=""):
        """Create a plan and its jobs. Returns {'plan', 'jobs', 'risk', 'gates'}."""
        text = f"{goal['title']} {goal.get('description') or ''}"
        workflow = workflow or goal.get("workflow") or infer_workflow(text)
        level = risk_level or risk_module.classify(
            text, stated=goal.get("risk"), pack=self.pack
        )
        subject = subject or goal["title"]

        plan = self.store.create_plan(goal["id"], workflow, rationale=rationale)
        default_policy = risk_module.review_policy(level)
        specialists = risk_module.specialist_roles(level, text)

        created, opened_gates = {}, []
        for step, depends in workflows.steps_for(workflow, level):
            job = self.store.create_job(
                goal["project_id"], workflow, step.role, subject,
                goal_id=goal["id"], plan_id=plan["id"],
                depends_on=[
                    (created[key]["id"],
                     _EDGE_THRESHOLDS.get((step.key, key), machine.DONE))
                    for key in depends if key in created
                ],
                risk=level,
                review_policy=step.review_policy or default_policy,
                scope=step.scope,
                non_goals=step.non_goals,
                output_contract=step.output_contract,
                acceptance_criteria=list(step.acceptance) or list(
                    goal.get("acceptance_criteria") or []),
                repo=repo, branch=branch, base_sha=base_sha, worktree=worktree,
                session_policy="fresh" if step.role in ("reviewer", "specialist", "evaluator")
                else "reuse",
                metadata={"step": step.key, "title": step.title,
                          "specialists": specialists if step.key == "specialist" else []},
            )
            created[step.key] = job
            opened_gates.extend(self._gates_for_step(goal, job, step, level, text))

        self._wire_targets(created)
        self.store.set_goal_status(goal["id"], "PLANNED")
        self.store.record_event(
            "plan.created",
            {"goal_id": goal["id"], "plan_id": plan["id"], "workflow": workflow,
             "risk": level, "jobs": [j["id"] for j in created.values()]},
            project_id=goal["project_id"],
        )
        jobs = [self.store.get_job(job["id"]) for job in created.values()]
        return {"plan": plan, "jobs": jobs, "risk": level,
                "workflow": workflow, "gates": opened_gates}

    def _wire_targets(self, created):
        """Point review/landing jobs at the candidate they act on."""
        build = created.get("build")
        if not build:
            return
        for key in _REVIEWS_BUILD:
            if key in created:
                self.store.update_job(created[key]["id"], reviews_job_id=build["id"])
        if "land" in created:
            self.store.update_job(created["land"]["id"], lands_job_id=build["id"])

    def _gates_for_step(self, goal, job, step, level, text):
        """CRITICAL work, and anything a pack flags, pauses before it starts."""
        if step.role not in ("build", "landing", "operator"):
            return []
        kinds = []
        if risk_module.requires_human_gate(level):
            kinds.append("destructive" if step.role != "operator" else "budget")
        kinds.extend(self.pack.gates_for(f"{text} {step.scope}"))

        opened = []
        for kind in dict.fromkeys(kinds):
            opened.append(gates.open_gate(
                self.store, kind,
                question=f"Approve {job['id']} ({step.title}) at risk {level}?",
                project_id=goal["project_id"], goal_id=goal["id"], job_id=job["id"],
                context=f"Goal: {goal['title']}\nScope: {step.scope}",
                resume_status=machine.PLANNED,
            ))
        return opened
