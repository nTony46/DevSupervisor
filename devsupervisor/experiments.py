"""Paired experiments and the pair lock.

An A/B comparison is only evidence if the two arms differ in exactly one thing.
Every other difference — a different model, a longer timeout, a different base
commit, a slightly reworded task — is an alternative explanation for whatever
the experiment appears to show.

So the locked fields are checked in code, at dispatch, before either arm costs
anything. The treatment lives in metadata, deliberately outside the locked set,
because it is the one thing that is supposed to differ.
"""

from . import clock, ids
from .errors import PolicyViolation
from .state import machine

# Everything that must be identical across arms. The task body is included:
# rewording one arm's prompt is the easiest way to fake a result by accident.
LOCKED_JOB_FIELDS = (
    "model", "effort", "permission_mode", "risk", "repo", "base_sha", "branch",
    "scope", "non_goals", "output_contract", "review_policy", "job_type",
)
LOCKED_METADATA_FIELDS = ("timeout_s", "tools", "max_budget_usd", "environment")


class ExperimentPairViolation(PolicyViolation):
    """Two arms of one experiment diverge in something that is not the treatment."""


def create_pair(store, project_id, subject, treatments, goal_id=None, plan_id=None,
                role="build", job_type="experiment", pair_id=None, **shared):
    """Create the arms of one experiment. Every arm gets identical inputs.

    `treatments` maps arm name -> the treatment description for that arm. The
    control arm is conventionally `{"control": None}`.
    """
    if len(treatments) < 2:
        raise ValueError("an experiment pair needs at least two arms")
    pair_id = pair_id or ids.new_id("pair")
    metadata = dict(shared.pop("metadata", {}) or {})
    arms = []
    for arm, treatment in treatments.items():
        arm_metadata = dict(metadata)
        arm_metadata["experiment"] = {
            "pair_id": pair_id, "arm": arm, "treatment": treatment,
            "created_at": clock.now_iso(),
        }
        arms.append(store.create_job(
            project_id, job_type, role, subject, goal_id=goal_id, plan_id=plan_id,
            metadata=arm_metadata, **shared))
    store.record_event("experiment.pair_created",
                       {"pair_id": pair_id, "arms": [job["id"] for job in arms]},
                       project_id=project_id)
    return {"pair_id": pair_id, "arms": arms}


def pair_id_of(job):
    return ((job.get("metadata") or {}).get("experiment") or {}).get("pair_id")


def pair_members(store, pair_id, project_id=None):
    return [job for job in store.list_jobs(project_id)
            if pair_id_of(job) == pair_id and job["status"] != machine.SUPERSEDED]


def locked_signature(job):
    """The fields that must match across arms, as a comparable mapping."""
    metadata = job.get("metadata") or {}
    signature = {field: job.get(field) for field in LOCKED_JOB_FIELDS}
    for field in LOCKED_METADATA_FIELDS:
        value = metadata.get(field)
        signature[f"metadata.{field}"] = tuple(value) if isinstance(value, list) else value
    return signature


def diff_pair(store, pair_id, project_id=None):
    """Fields where the arms disagree. Empty means the comparison is sound."""
    members = pair_members(store, pair_id, project_id)
    if len(members) < 2:
        return {}
    reference = locked_signature(members[0])
    differences = {}
    for member in members[1:]:
        signature = locked_signature(member)
        for field, value in reference.items():
            if signature.get(field) != value:
                differences.setdefault(field, {})[members[0]["id"]] = value
                differences[field][member["id"]] = signature.get(field)
    return differences


def lock_routing(store, job, model, effort, project_id=None, permission_mode=None):
    """Apply one routing decision to every arm of a pair.

    Routing is per role, and both arms share a role, so the decision is the same
    either way. Stamping it lazily at each arm's own dispatch would leave the
    second arm unrouted while the first is running, which reads as divergence and
    — worse — would let a policy change between dispatches split the pair.
    """
    pair_id = pair_id_of(job)
    if not pair_id:
        return []
    updated = []
    for member in pair_members(store, pair_id, project_id or job.get("project_id")):
        if (member["model"] != model or member["effort"] != effort
                or (permission_mode and member["permission_mode"] != permission_mode)):
            fields = {"model": model, "effort": effort}
            if permission_mode:
                fields["permission_mode"] = permission_mode
            store.update_job(member["id"], **fields)
            updated.append(member["id"])
    if updated:
        store.record_event("experiment.routing_locked",
                           {"pair_id": pair_id, "model": model, "effort": effort,
                            "permission_mode": permission_mode, "arms": updated},
                           project_id=job.get("project_id"), job_id=job["id"])
    return updated


def check_pair(store, job, project_id=None):
    """Refuse to dispatch an arm whose pair has drifted. Called before spending."""
    pair_id = pair_id_of(job)
    if not pair_id:
        return True
    differences = diff_pair(store, pair_id, project_id or job.get("project_id"))
    if differences:
        detail = "; ".join(f"{field}: {values}" for field, values in sorted(differences.items()))
        raise ExperimentPairViolation(
            f"experiment {pair_id} arms diverge outside the treatment ({detail}); "
            f"the comparison would not mean anything"
        )
    return True


def describe(store, pair_id, project_id=None):
    members = pair_members(store, pair_id, project_id)
    return {
        "pair_id": pair_id,
        "arms": [{"job_id": m["id"],
                  "arm": (m["metadata"]["experiment"])["arm"],
                  "treatment": (m["metadata"]["experiment"])["treatment"],
                  "model": m["model"], "effort": m["effort"], "status": m["status"]}
                 for m in members],
        "locked": locked_signature(members[0]) if members else {},
        "differences": diff_pair(store, pair_id, project_id),
    }
