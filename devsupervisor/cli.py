"""`devsup` — the local command line.

Every command reads and writes the same durable state the supervisor loop uses,
so the CLI and the loop can never disagree about what is happening.
"""

import argparse
import json
import signal
import sys

from . import __version__, artifacts, config, doctor, gates, gitfacts, metrics, retrospective
from .errors import DevSupervisorError, GracefulExit
from .handoffs import importer as handoff_importer
from .handoffs import parser as handoff_parser
from .handoffs import reconcile as handoff_reconcile
from .memory import candidates as memory_candidates
from .memory import curator
from .memory.store import MemoryStore
from .planner import Planner
from .policy import learnable, packs
from .policy.routing import ModelRouter
from .providers import resolve as resolve_provider
from .state import Store, leases, machine
from .supervisor import Supervisor

EXIT_OK, EXIT_ERROR, EXIT_INTERRUPTED = 0, 1, 130


def _install_signal_handler():
    def handler(signum, frame):
        raise GracefulExit()
    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except ValueError:                       # not on the main thread; tests
        pass


# --- commands -------------------------------------------------------------


def cmd_init(store, args):
    facts = gitfacts.facts(args.repo)
    if not facts["exists"]:
        raise DevSupervisorError(f"{args.repo} does not exist")
    if not facts["is_git"] and not args.allow_non_git:
        raise DevSupervisorError(
            f"{args.repo} is not a git repository. SHA-based review and landing cannot "
            f"apply there; pass --allow-non-git to register it anyway.")
    name = args.name or (facts.get("toplevel") or args.repo).rstrip("/").split("/")[-1]
    if store.get_project(name):
        raise DevSupervisorError(f"project {name!r} already exists")
    if args.pack:
        packs.load(args.pack)                # fail now rather than at dispatch
    project = store.create_project(name, facts.get("toplevel") or args.repo,
                                   policy_pack=args.pack)
    config.ensure_project_dirs(project["id"])
    print(f"initialised project {project['name']} at {project['repo_path']}")
    if facts["is_git"]:
        print(f"  branch {facts['branch']} @ {(facts['head'] or '')[:12]}"
              f"{'' if facts['clean'] else '  (working tree dirty)'}")
    return EXIT_OK


def cmd_goal_add(store, args):
    project = store.require_project(args.project)
    goal = store.create_goal(project["id"], args.title, description=args.description or "",
                             acceptance_criteria=args.criteria or [], risk=args.risk)
    print(f"{goal['id']}  {goal['title']}")
    return EXIT_OK


def cmd_goal_list(store, args):
    for goal in store.list_goals(args.project and store.require_project(args.project)["id"]):
        print(f"{goal['id']}  [{goal['status']:8}] {goal['title']}")
    return EXIT_OK


def cmd_plan(store, args):
    goal = store.get_goal(args.goal_id)
    if goal is None:
        raise DevSupervisorError(f"no such goal: {args.goal_id}")
    project = store.require_project(goal["project_id"])
    supervisor = _supervisor(store, project, args)
    result = supervisor.plan_goal(goal, workflow=args.workflow, risk_level=args.risk,
                                 repo=project["repo_path"])
    print(f"plan {result['plan']['id']}  workflow={result['workflow']}  risk={result['risk']}")
    for job in result["jobs"]:
        depends = ", ".join(store.dependencies(job["id"])) or "-"
        print(f"  {job['id']:44} {job['role']:13} review={job['review_policy']:22} <- {depends}")
    for gate in result["gates"]:
        print(f"  GATE {gate['id']} ({gate['kind']}): {gate['question']}")
    return EXIT_OK


def cmd_run(store, args):
    project = store.require_project(args.project)
    supervisor = _supervisor(store, project, args)
    if args.dry_run:
        return _print_dry_run(supervisor.dry_run_report(project["id"]))
    _install_signal_handler()
    try:
        summary = supervisor.run(project["id"], max_iterations=args.max_iterations)
    except GracefulExit:
        print("\ninterrupted; state is durable — `devsup resume` continues from here")
        return EXIT_INTERRUPTED
    print(f"dispatched {len(summary['dispatched'])} job(s) over "
          f"{summary['iterations']} iteration(s)")
    for job_id in summary["dispatched"]:
        print(f"  {job_id}")
    print(f"stopped: {summary['stopped_because']}")
    for gate in summary["open_gates"]:
        print(f"  GATE {gate['id']} ({gate['kind']}): {gate['question']}")
    return EXIT_OK


def cmd_resume(store, args):
    """Resume is `run` without needing to remember what was in flight."""
    projects = ([store.require_project(args.project)] if args.project
                else store.list_projects())
    if not projects:
        print("no projects registered")
        return EXIT_OK
    for project in projects:
        print(f"== {project['name']}")
        args.project = project["name"]
        cmd_run(store, args)
    return EXIT_OK


def cmd_status(store, args):
    for project in store.list_projects():
        facts = gitfacts.facts(project["repo_path"])
        location = (f"{facts.get('branch')} @ {(facts.get('head') or '')[:12]}"
                    if facts["is_git"] else "not a git repository")
        print(f"{project['name']}  ({location})")
        for goal in store.list_goals(project["id"]):
            print(f"  goal {goal['id']}  [{goal['status']}]  {goal['title']}")
        counts = {}
        for job in store.list_jobs(project["id"]):
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        print(f"  jobs: {counts or 'none'}")
        open_gates = gates.open_gates(store, project_id=project["id"])
        for gate in open_gates:
            print(f"  GATE {gate['id']} ({gate['kind']}): {gate['question']}")
        print(f"  spend: ${metrics.total_cost(store, project['id']):.2f}")
    lock = leases.supervisor_lock_status(store)
    if lock:
        print(f"supervisor lock: {lock['owner']}"
              f"{' (stale)' if lock['expired'] else ''}")
    return EXIT_OK


def cmd_jobs(store, args):
    project_id = args.project and store.require_project(args.project)["id"]
    for job in store.list_jobs(project_id, status=args.status, role=args.role):
        print(f"{job['id']:46} {job['status']:15} {job['role']:12} risk={job['risk']}")
    return EXIT_OK


def cmd_job_show(store, args):
    job = store.require_job(args.job_id)
    print(f"{job['id']}  [{job['status']}]  {job['role']}  risk={job['risk']}")
    print(f"  goal        {job['goal_id']}")
    print(f"  review      {job['review_policy']}   attempt={job['attempt']} "
          f"revisions={job['revision_count']}/{job['max_revisions']}")
    print(f"  routing     {job['model'] or '(unrouted)'} @ effort {job['effort'] or '-'}"
          f"  permission={job['permission_mode'] or '-'}")
    print(f"  branch/base {job['branch']} / {job['base_sha']}")
    print(f"  result sha  {job['result_sha']}")
    print(f"  scope       {job['scope']}")
    print(f"  non-goals   {job['non_goals']}")
    if job["blockers"]:
        print("  blockers:")
        for index, blocker in enumerate(job["blockers"], start=1):
            print(f"    {index}. {blocker}")
    print("  dependencies:")
    for edge in store.dependency_edges(job["id"]) or []:
        print(f"    {edge['depends_on']} (needs {edge['satisfied_by']})")
    print("  transitions:")
    for transition in store.transitions(job["id"]):
        print(f"    {transition['created_at']}  {transition['from_status'] or '-':14}"
              f" -> {transition['to_status']:14} {transition['actor']}"
              f"  {transition['reason']}")
    print("  artifacts:")
    for artifact in artifacts.for_job(store, job["id"]):
        print(f"    {artifacts.reference(artifact)}")
    return EXIT_OK


def cmd_pause(store, args):
    project_id = args.project and store.require_project(args.project)["id"]
    paused = []
    for job in store.list_jobs(project_id, status=[machine.READY, machine.PLANNED]):
        store.transition(job["id"], machine.PAUSED, actor="cli", reason=args.reason or "paused")
        paused.append(job["id"])
    print(f"paused {len(paused)} job(s)")
    return EXIT_OK


def cmd_approve(store, args):
    gate = gates.decide(store, args.gate_id, approved=True, actor=args.actor,
                        note=args.note or "")
    print(f"gate {gate['id']} {gate['status']} by {gate['decided_by']}")
    return EXIT_OK


def cmd_reject(store, args):
    gate = gates.decide(store, args.gate_id, approved=False, actor=args.actor,
                        note=args.note or "")
    print(f"gate {gate['id']} {gate['status']} by {gate['decided_by']}")
    return EXIT_OK


def cmd_gates(store, args):
    for gate in gates.open_gates(store):
        print(f"{gate['id']}  {gate['kind']:22} {gate['job_id'] or '-':44} {gate['question']}")
    return EXIT_OK


def cmd_doctor(store, args):
    checks = doctor.run(store)
    print(doctor.render(checks))
    return EXIT_OK if doctor.worst(checks) != doctor.FAIL else EXIT_ERROR


def cmd_import_handoffs(store, args):
    project = store.require_project(args.project) if args.project else None
    handoffs = handoff_parser.load_all(args.path)
    if not handoffs:
        raise DevSupervisorError(f"no handoff documents found in {args.path}")
    jobs = handoff_reconcile.reconcile(
        handoffs, repo=project["repo_path"] if project else None, main_ref=args.main_ref)
    summary = handoff_reconcile.summarize(jobs)
    if args.apply:
        if project is None:
            raise DevSupervisorError("--apply needs --project")
        applied = handoff_importer.import_jobs(
            store, project, jobs, pack=packs.load(project["policy_pack"]))
        print(f"imported into {project['name']}:")
        print(f"  review chains  {len(applied['review_chains'])}")
        print(f"  completed      {len(applied['completed'])}")
        print(f"  superseded     {len(applied['superseded'])}")
        print(f"  triage         {len(applied['triage'])}")
        print(f"  skipped        {len(applied['skipped'])}")
        for chain in applied["review_chains"]:
            print(f"  {chain['build']} -> {chain['reviewer']} -> {chain['landing']}"
                  f" -> {chain['evaluator']}  (risk {chain['risk']})")
        for entry in applied["skipped"]:
            print(f"  skipped {entry['key']}: {entry['reason']}")
        return EXIT_OK
    if args.json:
        print(json.dumps({"summary": summary, "jobs": [j.to_dict() for j in jobs]}, indent=2))
        return EXIT_OK
    print(f"{len(handoffs)} document(s) -> {summary['logical_jobs']} logical job(s)")
    for classification, count in summary["by_classification"].items():
        print(f"  {classification:15} {count}")
    print()
    for job in jobs:
        print(f"{job.classification:15} {job.key}")
        print(f"    sources: {', '.join(job.sources)}")
        if job.head:
            print(f"    head {job.head[:12]}  merged={job.verified.get('merged')}")
        for disagreement in job.disagreements:
            print(f"    ! {disagreement}")
    return EXIT_OK


def cmd_memory_curate(store, args):
    project = store.require_project(args.project)
    report, path = curator.curate(project["id"])
    print(f"reviewed {report['documents']} document(s)")
    print(f"  duplicates: {len(report['duplicates'])}  conflicts: {len(report['conflicts'])}"
          f"  stale: {len(report['stale'])}")
    print(f"proposal written to {path}")
    print("authoritative memory is unchanged; adopt with `devsup memory adopt`")
    return EXIT_OK


def cmd_memory_list(store, args):
    project = store.require_project(args.project)
    for document in MemoryStore(project["id"]).list():
        print(f"{document['relative_path']:52} {document['title']}")
    proposed = memory_candidates.list_candidates(project["id"])
    if proposed:
        print("\ncandidates (not authoritative):")
        for candidate in proposed:
            print(f"  {candidate['name']:52} {candidate['title']}")
    return EXIT_OK


def cmd_memory_adopt(store, args):
    project = store.require_project(args.project)
    written = memory_candidates.adopt(project["id"], args.name, actor=args.actor,
                                      overwrite=args.overwrite)
    print(f"adopted into {written}")
    return EXIT_OK


def cmd_retrospect(store, args):
    project = store.require_project(args.project)
    result = retrospective.retrospect(store, project["id"])
    print(retrospective.render(result["observations"], result["candidates"]))
    if result["memory_candidate"]:
        print(f"\nwritten as a memory candidate: {result['memory_candidate']}")
    return EXIT_OK


def cmd_routing(store, args):
    router = ModelRouter(store)
    print(router.render())
    return EXIT_OK


def cmd_permissions(store, args):
    from .policy import permissions
    print("execution policy (permission mode per role)\n")
    print(permissions.describe())
    print("\nbypass grants autonomy inside a disposable worktree. It grants no")
    print("authority: landing, pushing, history rewrite, benchmark mutation and")
    print("spend are decided by DevSupervisor before and after the worker runs.")
    shared = [p["repo_path"] for p in store.list_projects()]
    if shared:
        print("\nshared checkouts a bypassed worker will be withheld from:")
        for path in shared:
            print(f"  {path}")
    return EXIT_OK


def cmd_runs(store, args):
    for run in metrics.runs_for(store, args.job_id):
        print(f"{run['id']}  attempt={run['attempt']}  {run['status']:10} "
              f"{run['provider']}/{run['model'] or '-'} effort={run['effort'] or '-'}")
        print(f"    resolved={run['model_resolved'] or '-'}  routing={run['routing_source'] or '-'}"
              f"  verdict={run['review_outcome'] or '-'}")
        print(f"    permission={run['permission_mode'] or '-'} "
              f"bypass={'yes' if run['bypass_permissions'] else 'no'}  "
              f"worktree={run['worktree'] or '-'}")
        print(f"    tokens in/out={run['tokens_in'] or 0}/{run['tokens_out'] or 0}  "
              f"cost=${run['cost_usd'] or 0:.4f}  duration={run['duration_s'] or 0:.1f}s")
    return EXIT_OK


def cmd_policy_list(store, args):
    for policy in learnable.list_policies(store, status=args.status):
        print(f"{policy['id']}  {policy['name']:34} v{policy['version']} "
              f"{policy['status']:10} {policy['kind']}")
    return EXIT_OK


def cmd_policy_adopt(store, args):
    policy = learnable.adopt(store, args.policy_id, actor=args.actor)
    print(f"adopted {policy['name']} v{policy['version']}")
    return EXIT_OK


# --- helpers --------------------------------------------------------------


def _supervisor(store, project, args):
    provider = resolve_provider(
        getattr(args, "provider", None),
        allow_paid=getattr(args, "allow_paid", False),
        **({"budget_usd": args.budget} if getattr(args, "budget", None) else {}))
    pack = packs.load(project["policy_pack"])
    return Supervisor(store, provider=provider, pack=pack,
                      dry_run=getattr(args, "dry_run", False),
                      repo_facts=gitfacts.facts(project["repo_path"]))


def _print_dry_run(report):
    if not report["ready"]:
        print("no jobs are ready")
    for entry in report["ready"]:
        print(f"{entry['job_id']}  [{entry['role']}]  risk={entry['risk']} "
              f"review={entry['review_policy']}")
        print(f"    provider     {entry['provider']} "
              f"(session={entry['session_policy']})")
        print(f"    packet       {entry['packet_chars']} chars: "
              f"{', '.join(entry['packet_sections'])}")
        if entry["packet_sources"]:
            print(f"    cites        {', '.join(entry['packet_sources'])}")
        print(f"    reviewers    {', '.join(entry['intended_reviewers']) or '-'}")
        print(f"    landing      {', '.join(entry['intended_landing']) or '-'}")
    for entry in report["waiting_human"]:
        print(f"{entry['job_id']}  WAITING_HUMAN  gates={', '.join(entry['gates'])}")
    for job_id in report["blocked"]:
        print(f"{job_id}  BLOCKED")
    return EXIT_OK


# --- argument parsing -----------------------------------------------------


def build_parser():
    root = argparse.ArgumentParser(prog="devsup", description=__doc__.splitlines()[0])
    root.add_argument("--version", action="version", version=f"devsup {__version__}")
    subs = root.add_subparsers(dest="command", required=True)

    init = subs.add_parser("init", help="register a repository as a project")
    init.add_argument("repo")
    init.add_argument("--name")
    init.add_argument("--pack", help="project policy pack name")
    init.add_argument("--allow-non-git", action="store_true")
    init.set_defaults(func=cmd_init)

    goal = subs.add_parser("goal", help="goals").add_subparsers(dest="goal_command",
                                                                required=True)
    goal_add = goal.add_parser("add")
    goal_add.add_argument("project")
    goal_add.add_argument("title")
    goal_add.add_argument("--description")
    goal_add.add_argument("--criteria", nargs="*")
    goal_add.add_argument("--risk", choices=("LOW", "MEDIUM", "HIGH", "CRITICAL"))
    goal_add.set_defaults(func=cmd_goal_add)
    goal_list = goal.add_parser("list")
    goal_list.add_argument("--project")
    goal_list.set_defaults(func=cmd_goal_list)

    plan = subs.add_parser("plan", help="turn a goal into a job graph")
    plan.add_argument("goal_id")
    plan.add_argument("--workflow")
    plan.add_argument("--risk", choices=("LOW", "MEDIUM", "HIGH", "CRITICAL"))
    plan.set_defaults(func=cmd_plan)

    run = subs.add_parser("run", help="drive ready jobs")
    run.add_argument("project")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--provider")
    run.add_argument("--allow-paid", action="store_true")
    run.add_argument("--budget", type=float)
    run.add_argument("--max-iterations", type=int, default=200)
    run.set_defaults(func=cmd_run)

    resume = subs.add_parser("resume", help="continue every project from durable state")
    resume.add_argument("--project")
    resume.add_argument("--dry-run", action="store_true")
    resume.add_argument("--provider")
    resume.add_argument("--allow-paid", action="store_true")
    resume.add_argument("--budget", type=float)
    resume.add_argument("--max-iterations", type=int, default=200)
    resume.set_defaults(func=cmd_resume)

    status = subs.add_parser("status", help="what is happening")
    status.set_defaults(func=cmd_status)

    jobs = subs.add_parser("jobs", help="list jobs")
    jobs.add_argument("--project")
    jobs.add_argument("--status")
    jobs.add_argument("--role")
    jobs.set_defaults(func=cmd_jobs)

    job = subs.add_parser("job", help="job detail").add_subparsers(dest="job_command",
                                                                   required=True)
    job_show = job.add_parser("show")
    job_show.add_argument("job_id")
    job_show.set_defaults(func=cmd_job_show)

    pause = subs.add_parser("pause", help="park ready work")
    pause.add_argument("--project")
    pause.add_argument("--reason")
    pause.set_defaults(func=cmd_pause)

    for name, func in (("approve", cmd_approve), ("reject", cmd_reject)):
        decide = subs.add_parser(name, help=f"{name} a human gate")
        decide.add_argument("gate_id")
        decide.add_argument("--actor", default="human")
        decide.add_argument("--note")
        decide.set_defaults(func=func)

    gate_list = subs.add_parser("gates", help="list open human gates")
    gate_list.set_defaults(func=cmd_gates)

    routing = subs.add_parser("routing", help="show the resolved model routing policy")
    routing.set_defaults(func=cmd_routing)

    perms = subs.add_parser("permissions", help="show the execution policy per role")
    perms.set_defaults(func=cmd_permissions)

    runs = subs.add_parser("runs", help="run records for a job")
    runs.add_argument("job_id")
    runs.set_defaults(func=cmd_runs)

    doctor_cmd = subs.add_parser("doctor", help="check this installation")
    doctor_cmd.set_defaults(func=cmd_doctor)

    imports = subs.add_parser("import-handoffs", help="reconcile prior agent handoffs")
    imports.add_argument("path")
    imports.add_argument("--project")
    imports.add_argument("--main-ref", default="origin/main")
    imports.add_argument("--json", action="store_true")
    imports.add_argument("--apply", action="store_true",
                         help="create jobs for the reconciled work")
    imports.set_defaults(func=cmd_import_handoffs)

    memory = subs.add_parser("memory", help="project memory").add_subparsers(
        dest="memory_command", required=True)
    curate = memory.add_parser("curate")
    curate.add_argument("project")
    curate.set_defaults(func=cmd_memory_curate)
    memory_list = memory.add_parser("list")
    memory_list.add_argument("project")
    memory_list.set_defaults(func=cmd_memory_list)
    adopt = memory.add_parser("adopt")
    adopt.add_argument("project")
    adopt.add_argument("name")
    adopt.add_argument("--actor", default="human")
    adopt.add_argument("--overwrite", action="store_true")
    adopt.set_defaults(func=cmd_memory_adopt)

    retro = subs.add_parser("retrospect", help="observations and policy candidates")
    retro.add_argument("project")
    retro.set_defaults(func=cmd_retrospect)

    policy = subs.add_parser("policy", help="learnable policy").add_subparsers(
        dest="policy_command", required=True)
    policy_list = policy.add_parser("list")
    policy_list.add_argument("--status")
    policy_list.set_defaults(func=cmd_policy_list)
    policy_adopt = policy.add_parser("adopt")
    policy_adopt.add_argument("policy_id")
    policy_adopt.add_argument("--actor", default="human")
    policy_adopt.set_defaults(func=cmd_policy_adopt)

    return root


def main(argv=None):
    args = build_parser().parse_args(argv)
    config.ensure_home()
    store = Store.open()
    try:
        return args.func(store, args)
    except GracefulExit:
        print("\ninterrupted; state is durable")
        return EXIT_INTERRUPTED
    except DevSupervisorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        store.close()


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(main())
