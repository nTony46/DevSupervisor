# Bootstrap Log — campaign `devsupervisor-v1`

## Phase 1 — architecture and handoff reconciliation

- Normalized repo context read-only. Canonical `~/Desktop/Example` was clean on
  `eval/harness-v1`; switched to `main` (`6ca4ddd`) with a plain
  branch switch. Residue after the switch is `scripts/effectiveness/**/__pycache__`
  only — bytecode the eval branch's `.gitignore` covers and `main`'s does not.
  Nothing lost, nothing deleted. Six agent worktrees inspected read-only, all clean.
- Reconciled 19 pre-restart handoffs into a logical job inventory keyed on
  `(repo, branch, base_sha, result_sha, task)`. Every merge claim was re-verified
  against git; all checked out. Three separate sessions call themselves
  "Agent 6" and one file is misfiled — recorded as the evidence for
  agent-number-is-not-identity.
- Froze the architecture: deterministic harness vs. LLM judgment, job state
  machine, context compiler, memory-candidate curation, reviewer independence,
  leases, project policy packs.
- No Example product code touched.

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
