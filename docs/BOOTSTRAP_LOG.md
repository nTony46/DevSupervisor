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
