# Bootstrap Log — campaign `devsupervisor-v1`

## Phase 1 — architecture and handoff reconciliation

- Normalized repo context read-only. The target repository was clean on its
  harness branch; switched to `main` with a plain branch switch. Residue after
  the switch was `__pycache__` only — bytecode the harness branch's `.gitignore`
  covers and `main`'s does not. Nothing lost, nothing deleted. Six agent
  worktrees inspected read-only, all clean.
- Reconciled 19 pre-restart handoffs into a logical job inventory keyed on
  `(repo, branch, base_sha, result_sha, task)`. Every merge claim was re-verified
  against git; all checked out. Three separate sessions call themselves
  "Agent 6" and one file is misfiled — recorded as the evidence for
  agent-number-is-not-identity.
- Froze the architecture: deterministic harness vs. LLM judgment, job state
  machine, context compiler, memory-candidate curation, reviewer independence,
  leases, project policy packs.
- No product code in the target repository touched.

## Phase 2 — durable state engine

- SQLite schema for projects, goals, plans, jobs, dependencies, transitions,
  events, runs, provider sessions, artifacts, metrics, human gates, leases,
  policies, job relations, and the supervisor lock. WAL, foreign keys,
  `synchronous=FULL`, one transaction per transition.
- The state machine is a table plus guards, not prose. Guards are the rules the
  harness must own about the model rather than ask it: review is not skippable
  when policy requires it, an approver may not appear in the set of actors that
  did the work, landing needs a candidate SHA, revisions are capped.
- Readiness is a SQL `NOT EXISTS` over unfinished dependencies. Leases are rows
  with expiry; reclaiming one writes an event so a near-duplicate execution
  leaves a trace instead of vanishing.
- Restart proof runs a second interpreter against the on-disk database.
- 34 tests green.

## Phase 3 — context, memory, and artifacts

- Memory is path-addressed Markdown with dependency-free frontmatter, one topic
  per file, scoped to a project by construction. Writing over an existing
  document raises rather than replaces it.
- Redaction runs at every boundary that could make text durable or send it to a
  worker — import, memory write, candidate write, artifact write, packet
  compile — so a bug in one path cannot leak past the others. Memory *refuses*
  secret-shaped content; candidates *strip* it, because a candidate is exactly
  where an unreviewed claim belongs.
- Curation analyses duplicates, conflicts, and stale documents and writes a
  proposal into `memory-candidates/`. Authoritative memory is byte-identical
  afterwards; the test asserts that, not just the report.
- The context compiler builds eight scoped sections and cites every included
  document by path. Its non-goals are tested: no other project's memory, no
  transcripts, no builder monologue in a reviewer packet, blockers verbatim in a
  revision packet.
- A second interpreter rebuilds a worker's packet from artifacts alone with the
  first process's handles closed — the fresh-context handoff proof.
- 71 tests green.

## Phase 4 — planner, workflows, and policy

- Six workflow templates (feature, bug, refactor, research, experiment,
  migration). Risk selects which steps survive, and dropping a step rewires the
  steps that depended on it instead of orphaning them — at LOW risk a feature is
  build → land → evaluate; at HIGH it gains design and a specialist review.
- Risk is classified from the words used and the paths touched. A stated level
  can only raise the floor, never lower it. `goals.risk` is deliberately
  nullable: a NOT NULL default of MEDIUM silently overrode every classification,
  which the LOW-risk test caught.
- Immutable rules are constants with no mutation API; `learnable.propose`
  refuses any policy name that reaches for one. Learnable policy is versioned and
  adopted by a named actor.
- Policy packs are the only place a project name may appear, asserted by a test
  that greps the core. A test-local pack proves packs are pluggable without
  touching core.
- Human gates are rows. A job with an open gate is `WAITING_HUMAN` and cannot be
  promoted by readiness; every gate on a job must be decided before it resumes.
- 111 tests green.

## Phase 5 — agent runtime, scheduler, and the review loop

- Provider abstraction with a deterministic mock (every test runs on it) and a
  Claude Code headless adapter that refuses to run without an explicit opt-in
  and a budget. The adapter is tested without ever invoking it.
- Dependency edges gained a `satisfied_by` threshold. A reviewer cannot wait for
  the thing it reviews to be DONE, so its edge is satisfied at `UNDER_REVIEW`
  and landing's edge at `LANDING_READY`. This is the change that made the
  candidate/review/land lifecycle expressible as one DAG.
- Rejection creates a real revision job carrying exactly the blockers. The
  rejected attempt is SUPERSEDED, not deleted, and everything that depended on
  it is repointed — including a *fresh* review job, because a reviewer that has
  already ruled cannot re-review its own verdict.
- Approval is recorded with the reviewer's worker identity, so the independence
  guard has something real to compare against; the test asserts the approving
  actor is absent from the build job's work actors.
- Crash proof: the run stops after the reviewer approves, every handle is
  dropped, and a fresh store resumes at landing — dispatching exactly the
  landing and evaluator jobs and nothing already done.
- 140 tests green.

## Phase 6 — observability and self-improvement

- Coarse blocker and failure taxonomies. Fine categories are unstable; the point
  is to see a pattern across many jobs, not to label one blocker perfectly.
- Observations: first-pass review rate, blocker and failure categories, job size
  against rejection, review value per (job_type, risk) group, run statuses, spend.
- Three candidate rules — decomposition when large jobs are the ones being
  rejected, lighter review where review has never rejected, and a retry-strategy
  change when one failure category dominates. All refuse to fire below four
  samples: a policy derived from two data points is noise with a version number.
- The guardrail is structural rather than behavioural. The retrospective has no
  adopt path, and `learnable.propose` refuses any name reaching for an immutable
  rule, so a well-behaved retrospective and a misbehaving one are equally unable
  to weaken safety. Tested both ways.
- 153 tests green.

## Phase 7 — CLI and operational hardening

- `devsup` covers init, goal, plan, run, resume, status, jobs, job show, pause,
  approve/reject, gates, doctor, import-handoffs, memory curate/list/adopt,
  retrospect, and policy list/adopt. Paid providers need an explicit flag, so
  no command spends money by accident.
- Dry-run is genuinely read-only. `promotable()` was split out of
  `promote_ready()` so a dry run can ask the readiness question without
  changing the answer, and the test asserts job rows are byte-identical before
  and after two consecutive reports.
- `GracefulExit` is a BaseException on purpose: the scheduler converts a worker
  crash into a failed run, and an interrupt must not be recorded as one. It
  unwinds past that handler, releasing the job lease and the supervisor lock.
- Added orphan recovery. A lease expiring is one way a worker dies; being killed
  between releasing the lease and recording a result is another, and that left
  jobs stuck in RUNNING with no owner. Found by the interrupt-then-resume test.
- Tightened the redactor after the repo-hygiene test flagged `tokens_in=1000`.
  Key matching is now case-sensitive, values may not contain backticks (so prose
  naming `TOKEN=` is left alone), and lower-case keys need a long quoted value.
  A redactor that rewrites correct code is worse than one that misses a case.
- Generic behaviour is proven on `examples/sample_project`, a small pack-less
  repository the harness knows nothing about.
- 181 tests green.

## Phase 8 — project policy import and dry run

- The first project pack encodes exact-SHA review and landing, frozen-benchmark
  immutability, the clean-room boundary around oracle and held-out data, the
  harness-versus-product commit-identity split, correctness-first effectiveness
  claims, and budget/benchmark gates. Nothing of it is in the core, which a test
  greps to confirm.
- The importer reconstructs verifiable work only. Finished-but-unreviewed
  candidates become review → landing → evaluation chains against their exact
  SHA; merged work is imported as DONE so it is never proposed again; a claim
  with no branch and no SHA becomes a BLOCKED triage job rather than an
  assertion.
- Two handoff claims were checked rather than believed, and the check found a
  subtlety worth keeping: one fix branch reads as *not* landed against current
  `main`, because files it touched changed again in a later commit, and as
  landed against the SHA the handoff actually names.
  `landed_by_content(..., against=...)` exists for that distinction.
- Verification now runs over every handoff in a group, not just the most
  detailed one. A false claim in a secondary document is precisely what is worth
  surfacing, and it was invisible while only the best source was checked.
- Dry run over the reconstructed state offers two reviewer jobs, each naming its
  exact candidate SHA, plus six open human gates. It dispatches nothing, records
  no runs, and the target repository's HEAD, status, and every ref are compared
  before and after.
- 206 tests green.

## Follow-up — model routing, experiment pair lock, delegation ownership

- Model id and effort are discovered from the local installation
  (`~/.claude/settings.json` `modelSettings`, then the CLI's own `--effort` help
  text), never hardcoded. On this machine that resolves to `claude-opus-5` at
  effort `high`, and the resolution source is recorded on every run.
- Opening policy routes all fourteen roles to Opus at high effort, mechanical
  ones included. Critical roles — supervisor, reviewer, specialist, security,
  evaluator, benchmark — have an immutable floor enforced both at proposal time
  and at use, and are never given a fallback model.
- Schema v2 adds `runs.model_resolved/effort/routing_source/tools/
  max_budget_usd/review_outcome` and `jobs.effort`, applied as idempotent column
  additions so the live runtime root upgraded in place with all 28 jobs intact.
- Writing the pair-lock test found a real flaw: routing was stamped on each arm
  at its own dispatch, leaving the second arm unrouted while the first ran. That
  reads as divergence, and would let a policy change between dispatches split a
  pair silently. Routing is now locked across every arm before either runs.
- Workers return `subtask_requests`; only the supervisor creates jobs. The
  enforcement is structural — the provider contract has no store handle — and
  `delegation.py` is the single path from a request to a row.
- 259 tests green.
