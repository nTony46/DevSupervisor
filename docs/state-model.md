# State Model

All execution state lives in SQLite at `$DEVSUPERVISOR_HOME/supervisor.db`
(default `~/.devsupervisor/`). WAL mode, foreign keys on, every transition in a
transaction.

## Tables

| Table | Holds |
|---|---|
| `projects` | id, name, repo_path, policy_pack, created_at |
| `goals` | id, project_id, title, description, acceptance_criteria, status |
| `plans` | id, goal_id, version, workflow, status, rationale |
| `jobs` | the unit of work — see below |
| `job_dependencies` | (job_id, depends_on_job_id) edges of the DAG |
| `job_transitions` | append-only audit of every state change with reason + actor |
| `events` | append-only event log with `idempotency_key UNIQUE` |
| `runs` | one attempt of one job by one provider session |
| `provider_sessions` | provider, external session id, status, reuse policy |
| `artifacts` | kind, uri/path, sha256, job_id, created_at |
| `metrics` | per-run numeric/JSON measurements |
| `human_gates` | id, job_id, kind, question, status, decided_by, decided_at |
| `leases` | job ownership: owner, token, acquired_at, expires_at |
| `policies` | learnable orchestration policy, versioned, with adoption state |
| `job_relations` | duplicate/superseded links between logical jobs |

## The job row

Required fields (all persisted):

```
job_id            stable, human-meaningful: BUILD-line-range-001
project_id        goal_id            plan_id
job_type          feature|bug|refactor|research|experiment|migration|...
role              builder|reviewer|qa|security|landing|evaluator|...
status            see state machine
priority          risk              LOW|MEDIUM|HIGH|CRITICAL
repo  worktree  base_sha  branch
scope             what this job may touch
non_goals         what it must not touch
acceptance_criteria
output_contract   what the worker must produce
review_policy     none|independent|independent+specialist|human
attempt           revision_of        max_attempts
provider  model  session_id
artifact_refs     created_at  updated_at
```

### Stable job IDs, disposable agents

`BUILD-line-range-001` is the durable identity. The process, the session id, and
"Agent 4" are metadata. Reconciliation of prior work therefore keys on
(repo, branch, base_sha, result_sha, task) — never on an agent number. The
pre-restart Example handoffs prove why: three different sessions all called
themselves "Agent 6", and one file labelled `agent6-*` is a report about a
different project entirely.

## Job state machine

```
PLANNED ─► READY ─► DISPATCHED ─► RUNNING ─► WORK_COMPLETE ─► UNDER_REVIEW
                                                                  │
                                        ┌─────────────────────────┴──────────┐
                                        ▼                                    ▼
                                    REJECTED                             APPROVED
                                        │                                    │
                                 REVISION_READY                       LANDING_READY
                                        │                                    │
                                        └──────► READY                   LANDING
                                                                             │
                                                                          VERIFIED
                                                                             │
                                                                         EVALUATED
                                                                             │
                                                                           DONE
```

Off-graph states reachable from most live states:
`BLOCKED`, `FAILED`, `CANCELLED`, `SUPERSEDED`, `DUPLICATE`, `WAITING_HUMAN`,
`PAUSED`.

Rules the harness enforces, not the model:

- Any transition not in the table raises `IllegalTransition`. Loudly. No repair.
- `WORK_COMPLETE → APPROVED` directly is illegal when `review_policy != none`;
  the job must pass through `UNDER_REVIEW`.
- The actor recorded on `APPROVED` must not be the actor recorded on the
  `WORK_COMPLETE` of the same job (reviewer independence, enforced in code).
- `LANDING` requires an approved candidate SHA that still exists.
- `WAITING_HUMAN` can only be left by a decided `human_gates` row.
- Every transition writes a `job_transitions` row: from, to, actor, reason, ts.

## Dependency readiness

A `PLANNED` job becomes `READY` only when every `depends_on_job_id` is in a
terminal-success state (`DONE`, or `VERIFIED`/`EVALUATED` where the plan says
so). Readiness is computed by a SQL query, never by a model.

## Leases and duplicate execution

Dispatch acquires a lease: `(job_id, owner, token, expires_at)`, inserted with a
uniqueness constraint on `job_id`. A second worker cannot take a leased job. A
lease past `expires_at` is reclaimable — the reclaim writes an event so the
duplicate risk is auditable rather than silent.

If two results arrive for one logical job anyway, both artifacts are preserved;
one is marked authoritative and the other gets a `job_relations` row of kind
`DUPLICATE` or `SUPERSEDED`. Nothing is deleted.

## Idempotency and crash safety

Every externally-triggered event carries an `idempotency_key`. Re-delivery is a
no-op returning the original outcome. Combined with WAL and single-statement
transitions, this makes "crash then restart" safe: the scheduler recomputes
readiness from the database and resumes at the first incomplete job.
