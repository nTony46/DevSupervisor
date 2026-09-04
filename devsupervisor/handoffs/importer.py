"""Turn reconciled handoff work into real jobs.

Completed work is imported as completed, so the supervisor never proposes doing
it again. Work that is finished but unreviewed is imported as a *review chain*:
the candidate exists, so what remains is a review, a landing, and a goal check —
not a rebuild.
"""

from .. import config
from ..policy import risk as risk_module
from ..policy.packs import PolicyPack
from ..state import machine

# Classification -> what the supervisor should do about it.
DIRECT_STATUS = {
    "COMPLETE": machine.DONE,
    "SUPERSEDED": machine.SUPERSEDED,
    "PAUSED": machine.PAUSED,
    "BLOCKED": machine.BLOCKED,
    "ACTIVE": machine.BLOCKED,
    "UNVERIFIED": machine.BLOCKED,
}


def import_jobs(store, project, logical_jobs, pack=None, actor="handoff-import"):
    """Create jobs for reconciled work. Returns a report of what was imported."""
    pack = pack or PolicyPack()
    config.ensure_project_dirs(project["id"])
    report = {"review_chains": [], "completed": [], "superseded": [],
              "triage": [], "skipped": []}

    for gate in _pack_decisions(store, project, pack):
        report.setdefault("gates", []).append(gate["id"])

    for logical in logical_jobs:
        if logical.classification == "UNKNOWN":
            report["skipped"].append({
                "key": logical.key,
                "reason": (logical.disagreements[0] if logical.disagreements
                           else "state could not be determined"),
            })
            continue
        if logical.classification == "WAITING_REVIEW":
            report["review_chains"].append(
                _import_review_chain(store, project, logical, pack, actor))
            continue

        job = _import_simple(store, project, logical, pack, actor)
        bucket = {"COMPLETE": "completed", "SUPERSEDED": "superseded"}.get(
            logical.classification, "triage")
        report[bucket].append(job["id"])
    return report


def _pack_decisions(store, project, pack):
    """Open the decisions a project pack knows are outstanding.

    These are questions the repository cannot answer — which is exactly the test
    for whether something belongs in front of a human.
    """
    from .. import gates
    opened = []
    for kind, question, context in getattr(pack, "open_decisions", ()):
        if any(g["question"] == question for g in gates.open_gates(store,
                                                                   project_id=project["id"])):
            continue
        opened.append(gates.open_gate(store, kind, question,
                                      project_id=project["id"], context=context))
    return opened


def _subject(logical):
    return (logical.branch or logical.key.split(":", 1)[-1]).replace("/", " ")


def _risk_for(logical, pack):
    text = f"{logical.title} {logical.branch or ''}"
    return risk_module.classify(text, pack=pack)


def _common_fields(logical, level):
    return {
        "risk": level,
        "branch": logical.branch,
        "base_sha": logical.base,
        "result_sha": logical.head,
        "metadata": {
            "imported_from": logical.sources,
            "claimed_status": logical.claimed_status,
            "verified": logical.verified,
            "disagreements": logical.disagreements,
        },
    }


def _import_simple(store, project, logical, pack, actor):
    level = _risk_for(logical, pack)
    status = DIRECT_STATUS.get(logical.classification, machine.BLOCKED)
    scope = _scope_for(logical)
    job = store.create_job(
        project["id"], "imported", "build", _subject(logical),
        status=status, review_policy="none", scope=scope,
        non_goals="Do not redo work that is already recorded as complete.",
        output_contract="", **_common_fields(logical, level))
    if logical.landed_as:
        store.record_event(
            "handoff.superseded",
            {"job_id": job["id"], "branch": logical.branch,
             "landed_as": logical.landed_as,
             "verified": logical.verified.get("landed_by_content")},
            project_id=project["id"], job_id=job["id"])
    return job


def _scope_for(logical):
    if logical.classification == "SUPERSEDED":
        return (f"Historical. {logical.branch} is unmerged by SHA but its content is "
                f"already in the target"
                + (f" as {logical.landed_as}." if logical.landed_as else ".")
                + " Do not re-land it.")
    if logical.classification == "COMPLETE":
        return f"Complete and merged: {logical.branch or logical.title}."
    return (f"Triage required: {logical.title}. The handoff states "
            f"'{logical.claimed_status}' but names no branch or SHA that can be "
            f"verified, so its real state is unknown.")


def _import_review_chain(store, project, logical, pack, actor):
    """A finished-but-unreviewed candidate needs review, landing, and a goal check."""
    level = _risk_for(logical, pack)
    policy = risk_module.review_policy(level)
    subject = _subject(logical)
    goal = store.create_goal(
        project["id"], f"Review and land {logical.branch}",
        description=(f"Imported from {', '.join(logical.sources)}. The candidate exists "
                     f"at {logical.head[:12] if logical.head else 'an unknown sha'} and "
                     f"has never been through this harness."),
        acceptance_criteria=[
            "an independent reviewer approves the exact candidate SHA",
            "the landing job records the final target SHA",
            "required verification passes on the landed tree",
        ], risk=level)

    fields = _common_fields(logical, level)
    build = store.create_job(
        project["id"], "imported", "build", subject, goal_id=goal["id"],
        status=machine.WORK_COMPLETE, review_policy=policy,
        scope=f"Candidate already produced on {logical.branch}; nothing to build.",
        non_goals="Do not rewrite the candidate. It is under review as it stands.",
        output_contract="the existing candidate SHA", **fields)

    reviewer = store.create_job(
        project["id"], "imported", "reviewer", subject, goal_id=goal["id"],
        depends_on=[(build["id"], machine.UNDER_REVIEW)], risk=level,
        review_policy="none", reviews_job_id=build["id"], session_policy="fresh",
        branch=logical.branch, base_sha=logical.base,
        scope=("Review the exact candidate SHA against its contract. Return APPROVE, "
               "or REJECT with numbered blockers."),
        non_goals="Do not fix the code you are reviewing.",
        acceptance_criteria=[
            f"the candidate SHA {logical.head[:12] if logical.head else '(unknown)'} "
            f"still exists and is what was inspected",
            "every blocker is concrete and reproducible, not a matter of taste",
            "the verdict is APPROVE or REJECT with numbered blockers",
        ],
        output_contract="verdict APPROVE|REJECT, blockers[] when REJECT, evidence[]")

    landing = store.create_job(
        project["id"], "imported", "landing", subject, goal_id=goal["id"],
        depends_on=[(build["id"], machine.LANDING_READY), (reviewer["id"], machine.DONE)],
        risk=level, review_policy="none", lands_job_id=build["id"],
        branch=logical.branch, base_sha=logical.base,
        scope=("Verify the approved SHA still exists and matches what was reviewed, "
               "detect target-branch movement, land with safe git operations, run the "
               "required verification, record the final target SHA."),
        non_goals="Never force push. Never rewrite history. Invent no unrelated fixes.",
        acceptance_criteria=[
            "the approved SHA is verified present before anything is landed",
            "target-branch movement since review is detected and reported",
            "required verification passes on the landed tree",
            "the final target SHA is recorded",
        ],
        output_contract="landing report with the final target SHA")

    evaluator = store.create_job(
        project["id"], "imported", "evaluator", subject, goal_id=goal["id"],
        depends_on=[(landing["id"], machine.DONE)], risk=level, review_policy="none",
        reviews_job_id=build["id"], session_policy="fresh",
        scope="Did landing this candidate satisfy the goal's acceptance criteria?",
        output_contract="verdict APPROVE|REJECT against the goal criteria")

    store.transition(build["id"], machine.UNDER_REVIEW, actor=actor,
                     reason="imported: candidate exists and has never been reviewed")
    store.set_goal_status(goal["id"], "PLANNED")
    return {"goal_id": goal["id"], "build": build["id"], "reviewer": reviewer["id"],
            "landing": landing["id"], "evaluator": evaluator["id"], "risk": level}
