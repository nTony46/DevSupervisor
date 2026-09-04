"""Who is allowed to create a job.

Only the supervisor. A worker that can spawn its own children can also spawn a
reviewer of its own work, fan out without a budget, and produce a job graph
nobody planned. So a worker returns *requests* in its structured result, and the
supervisor decides — applying the same risk, review, and routing policy it
applies to everything else.

The enforcement is structural: a worker has no store handle. This module is the
only path from a request to a row, and it records who asked.
"""

from . import gates
from .errors import PolicyViolation
from .policy import risk as risk_module
from .policy.packs import PolicyPack
from .state import machine

# What a worker may ask for. A worker cannot request "landing": nothing that
# writes to a shared branch is delegated on a worker's say-so.
REQUESTABLE_ROLES = frozenset({
    "investigator", "researcher", "architect", "qa", "security", "specialist",
    "reviewer", "build",
})

# Supervisor-initiated fan-out may create anything.
FANOUT_ROLES = REQUESTABLE_ROLES | {"evaluator", "operator", "planner", "landing"}

MAX_REQUESTS_PER_RESULT = 4
MAX_FANOUT = 8


class DelegationRefused(PolicyViolation):
    """A subtask request was not something the supervisor will create."""


def _validate(request, allowed_roles):
    if not isinstance(request, dict):
        raise DelegationRefused(f"subtask request must be an object, got {type(request).__name__}")
    role = request.get("role")
    if role not in allowed_roles:
        raise DelegationRefused(
            f"role {role!r} may not be requested by a worker; allowed: {sorted(allowed_roles)}")
    if not request.get("scope"):
        raise DelegationRefused(f"subtask request for {role!r} has no scope")
    return role


def authorize(store, parent_job, requests, pack=None, actor="supervisor",
              max_requests=MAX_REQUESTS_PER_RESULT):
    """Create the subtasks a worker asked for — as the supervisor, or not at all."""
    requests = list(requests or [])
    if not requests:
        return []
    pack = pack or PolicyPack()
    created = []

    if len(requests) > max_requests:
        gates.open_gate(
            store, "strategy",
            f"{parent_job['id']} requested {len(requests)} subtasks (cap {max_requests}). "
            f"Approve the fan-out?",
            project_id=parent_job["project_id"], goal_id=parent_job["goal_id"],
            job_id=parent_job["id"], resume_status=machine.READY,
            context="\n".join(str(r.get("scope", "")) for r in requests))
        store.record_event("subtask.capped",
                           {"parent": parent_job["id"], "requested": len(requests)},
                           project_id=parent_job["project_id"], job_id=parent_job["id"])
        return []

    for request in requests:
        role = _validate(request, REQUESTABLE_ROLES)
        created.append(_create(store, parent_job, request, role, pack, actor,
                               requested_by=parent_job["id"]))
    return created


def fan_out(store, parent_job, specs, pack=None, actor="supervisor", max_children=MAX_FANOUT):
    """Supervisor-initiated parallel children, fanning results back to the parent."""
    specs = list(specs or [])
    if len(specs) > max_children:
        raise DelegationRefused(
            f"fan-out of {len(specs)} exceeds the cap of {max_children}")
    created = []
    for spec in specs:
        role = _validate(spec, FANOUT_ROLES)
        created.append(_create(store, parent_job, spec, role, pack or PolicyPack(), actor,
                               requested_by=None))
    store.record_event("delegation.fanned_out",
                       {"parent": parent_job["id"], "children": [c["id"] for c in created]},
                       project_id=parent_job["project_id"], job_id=parent_job["id"])
    return created


def _create(store, parent_job, spec, role, pack, actor, requested_by):
    level = risk_module.classify(
        f"{spec.get('scope', '')} {spec.get('non_goals', '')}",
        stated=spec.get("risk"), pack=pack)
    metadata = dict(spec.get("metadata") or {})
    metadata.update({
        "parent_job": parent_job["id"],
        "requested_by": requested_by,
        "authorized_by": actor,
        "reason": spec.get("reason", ""),
    })
    job = store.create_job(
        parent_job["project_id"], parent_job["job_type"], role,
        spec.get("subject") or _subject(parent_job),
        goal_id=parent_job["goal_id"], plan_id=parent_job["plan_id"],
        risk=level,
        review_policy=spec.get("review_policy") or risk_module.review_policy(level),
        scope=spec["scope"], non_goals=spec.get("non_goals", ""),
        acceptance_criteria=spec.get("acceptance_criteria") or [],
        output_contract=spec.get("output_contract", ""),
        repo=parent_job["repo"], branch=parent_job["branch"],
        base_sha=parent_job["base_sha"], worktree=parent_job["worktree"],
        session_policy="fresh", metadata=metadata,
        reviews_job_id=parent_job["id"] if role in ("reviewer", "specialist", "security")
        else None,
    )
    store.record_event(
        "subtask.authorized",
        {"parent": parent_job["id"], "child": job["id"], "role": role,
         "requested_by": requested_by, "authorized_by": actor},
        project_id=parent_job["project_id"], job_id=job["id"])
    return job


def _subject(job):
    parts = job["id"].split("-")
    return " ".join(parts[1:-1]) or job["id"]
