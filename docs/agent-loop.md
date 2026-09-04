# Agent Loops

## Inner loop — one worker, one job

```
OBSERVE          read the compiled job packet + the real repo
   │
THINK/PLAN       decide an approach inside the job's scope
   │
ACT WITH TOOLS   edit, run, build, test, measure
   │
OBSERVE TRUTH    read compiler output, test results, git diff, runtime behavior
   │
VERIFY           check against this job's acceptance_criteria
   │
   └─► not met ──► loop (bounded)
   └─► met ──────► write artifacts + structured result, exit
```

The worker is **disposable**. It owns no state the supervisor cannot rebuild
from its artifacts. It never becomes the project manager: it does not create
jobs, does not decide review policy, does not land its own work.

Ground truth beats assertion. A worker's report is accepted only alongside the
evidence it names — a test log, a diff, a SHA, a benchmark manifest. "I think it
works" is a `FAILED` verification, not a pass.

## Outer loop — the supervisor

```
OBSERVE PROJECT STATE     read SQLite; no conversation replay
   │
PLAN / REPLAN             ensure a current plan DAG exists for the goal
   │
SELECT READY JOBS         dependency-satisfied, unleased, under concurrency caps
   │
DELEGATE                  compile packet → provider.run() → lease held
   │
COLLECT                   ingest structured result + artifacts
   │
ROUTE                     review policy decides: reviewer / QA / straight to land
   │
REVISE OR LAND            rejection → targeted revision job (bounded)
   │                      approval  → landing job with the exact approved SHA
VERIFY                    landing runs required verification, records final SHA
   │
EVALUATE GOAL             separate evaluator asks: did this satisfy the goal?
   │
UPDATE                    memory candidates, metrics, plan
   │
REPEAT or STOP            no ready jobs / gate open / budget hit / goal DONE
```

## Context reset vs. session continuation

Provider session ids are persisted, but a conversation is never kept alive as a
matter of course.

**Reuse the session** when the follow-up is tightly scoped and the existing
context is an asset:
- reviewer asked for a small targeted revision of code this session just wrote;
- a clarification within the same job contract;
- context is still small and on-topic.

**Start a fresh context with a structured handoff** when:
- the task changes materially, or the role changes;
- the context has grown large or polluted with dead ends;
- independent review is required (the reviewer must not inherit the builder's
  reasoning — that is what makes it independent);
- a clean-room boundary is required (benchmark, held-out data, security review).

The rule is enforced in code: a reviewer job for job *J* is dispatched with
`session_id = NULL` and a packet that contains the candidate artifact, the
contract, and nothing from the builder's transcript.

## Contract before implementation

For MEDIUM risk and above, a short **job contract** is produced and persisted
before code begins:

```
WILL BUILD:      ...
OUT OF SCOPE:    ...
DONE MEANS:      ...
VERIFIED BY:     ...
```

The reviewer may tighten or reject the contract before build. Contracts are kept
at the level of outcomes: they do not dictate implementation details a competent
worker can safely choose itself.
