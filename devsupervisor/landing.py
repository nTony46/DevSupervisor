"""The deterministic landing contract.

Landing is the one step that writes to shared state, so what it may do is
decided here — in code, before and after the worker runs — rather than by the
worker's own judgment or by whatever tools it holds. A landing worker with every
permission in the world still cannot land something that was not approved, and
still cannot leave `main` in a state that is not a fast-forward of where it
started.
"""

from . import gitfacts
from .errors import PolicyViolation
from .policy import immutable
from .state import machine


def approvals_for(store, job_id):
    """Who recorded an approval of this candidate, and when."""
    return [t for t in store.transitions(job_id) if t["to_status"] == machine.APPROVED]


def resolve_target_ref(repo, preferred="origin/main"):
    """The ref landing will actually touch.

    A project with no remote is not a project that failed to land; it is a
    project without `origin/main`. Fall back rather than reporting a failure that
    is really a missing ref.
    """
    for ref in (preferred, "main", "master", "HEAD"):
        if ref and gitfacts.rev_parse(repo, ref):
            return ref
    return None


def preconditions(store, landing_job, main_ref="origin/main"):
    """Everything that must be true before a landing worker starts.

    Returns the contract, and records it on the job so the after-check compares
    against what was actually true at dispatch rather than re-deriving it later.
    """
    target_id = landing_job.get("lands_job_id")
    if not target_id:
        raise PolicyViolation(f"{landing_job['id']} is a landing job with no target")
    target = store.require_job(target_id)

    if not machine.reached(target["status"], machine.LANDING_READY):
        raise PolicyViolation(
            f"{target['id']} is {target['status']}, not ready to land; landing requires "
            f"an approved candidate")

    approvals = approvals_for(store, target_id)
    immutable.check_approval(target, approvals)

    approved_sha = target.get("result_sha")
    if not approved_sha:
        raise PolicyViolation(f"{target['id']} has no approved candidate sha")

    repo = landing_job.get("worktree") or landing_job.get("repo")
    contract = {
        "target_job": target_id,
        "approved_sha": approved_sha,
        "approved_by": [a["actor"] for a in approvals],
        "main_ref": main_ref,
        "repo": repo,
    }
    if repo and gitfacts.is_repo(repo):
        if not gitfacts.commit_exists(repo, approved_sha):
            raise PolicyViolation(
                f"approved candidate {approved_sha[:12]} is not present in {repo}")
        resolved_ref = resolve_target_ref(repo, main_ref)
        contract["main_ref"] = resolved_ref
        contract["main_before"] = (gitfacts.rev_parse(repo, resolved_ref)
                                   if resolved_ref else None)
        contract["changed_paths"] = gitfacts.changed_paths(repo, approved_sha)
        if resolved_ref:
            contract["already_landed"] = gitfacts.is_ancestor(repo, approved_sha,
                                                              resolved_ref)

    store.update_job(landing_job["id"],
                     metadata=dict(landing_job["metadata"], landing_contract=contract))
    return contract


def verify(store, landing_job, reported_sha, main_ref="origin/main"):
    """Everything that must be true after it. Raises rather than reporting success.

    Three independent checks, because a landing can fail in three different ways
    that all look like success from inside the worker: it landed something else,
    it rewrote history, or it landed a different patch than the one reviewed.
    """
    contract = (landing_job.get("metadata") or {}).get("landing_contract") or {}
    repo = contract.get("repo") or landing_job.get("worktree") or landing_job.get("repo")
    approved = contract.get("approved_sha")
    if not (repo and approved and gitfacts.is_repo(repo)):
        return {"checked": False, "reason": "no repository to verify against"}

    main_ref = contract.get("main_ref") or resolve_target_ref(repo, main_ref)
    if not main_ref:
        # Unverified is not verified. Say so, and let the caller record it.
        return {"checked": False,
                "reason": "no target ref could be resolved; landing is unverified"}

    main_after = gitfacts.rev_parse(repo, main_ref) or reported_sha
    findings = {"checked": True, "main_ref": main_ref,
                "main_before": contract.get("main_before"),
                "main_after": main_after, "approved_sha": approved}

    # 1. The approved work is actually in the target now.
    if not gitfacts.is_ancestor(repo, approved, main_ref):
        raise PolicyViolation(
            f"landing did not put {approved[:12]} into {main_ref}; "
            f"{main_ref} is now {(main_after or '?')[:12]}")

    # 2. History was extended, not rewritten. This is what makes "never force
    #    push" checkable instead of merely instructed.
    before = contract.get("main_before")
    if before and not gitfacts.is_ancestor(repo, before, main_ref):
        raise PolicyViolation(
            f"{main_ref} no longer contains its own previous tip {before[:12]}; "
            f"history was rewritten")

    # 3. The patch that landed is the patch that was reviewed.
    reviewed_paths = contract.get("changed_paths") or []
    if reviewed_paths:
        differing = gitfacts.landed_by_content(repo, approved, main_ref)
        findings["patch_identity"] = differing
        if differing.get("landed_by_content") is False:
            raise PolicyViolation(
                f"the tree at {main_ref} differs from the reviewed candidate on "
                f"{differing.get('paths_still_differing')}; that is not the patch "
                f"that was approved")
    return findings
