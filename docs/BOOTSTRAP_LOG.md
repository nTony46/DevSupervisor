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
