# DevSupervisor — Architecture

DevSupervisor turns a high-level engineering goal into a durable plan, delegates
work to disposable specialist agents, independently reviews their output, handles
targeted revisions, lands approved work, verifies the outcome, and survives
restarts.

## The governing principle

**The conversation is not the source of truth.**

A model context window is volatile, unbounded in cost, and unauditable. So the
model is treated as a *reasoning engine plugged into a durable deterministic
harness*. Everything that must survive a crash, a context reset, or a laptop
restart lives outside the model.

```
        ┌──────────────────────────────────────────────┐
        │  DETERMINISTIC HARNESS (Python, no model)    │
        │  state machine · scheduler · leases · gates  │
        │  policy · persistence · metrics · CLI        │
        └───────────────┬──────────────────────────────┘
                        │ compiled job packets  ▲ structured results
                        ▼                       │
        ┌──────────────────────────────────────────────┐
        │  PROVIDER (mock | claude-cli | future)       │
        │  disposable agent processes                  │
        └──────────────────────────────────────────────┘
```

## What is deterministic vs. what is LLM-driven

Deterministic code owns everything where a wrong answer is a correctness bug:

| Deterministic (code)                | LLM (judgment)                          |
|-------------------------------------|-----------------------------------------|
| job state transitions               | goal decomposition into jobs            |
| dependency readiness                | technical planning inside a job         |
| retry bounds and backoff            | interpreting a worker's report          |
| lease acquisition / duplicate guard  | whether evidence warrants replanning    |
| reviewer-independence enforcement    | which memory is relevant to a job       |
| human gate evaluation               | choosing among safe reversible options  |
| landing preconditions (SHA identity) | writing the code / review / analysis    |
| secret redaction                    | synthesizing a retrospective            |
| metrics and cost accounting         | naming a blocker category               |

The LLM never decides *whether a transition is legal*, *whether a job may skip
review*, or *whether a gate is satisfied*. It proposes; the harness disposes.

## Layers

1. **`state/`** — SQLite. Projects, goals, plans, jobs, dependencies,
   transitions, runs, sessions, artifacts, metrics, gates, leases, events.
   Single writer per process, WAL, transactional transitions.
2. **`memory/`** — path-addressed Markdown under the runtime root. Small focused
   documents, not one giant file. Worker claims land in `memory-candidates/`;
   promotion to authoritative memory is a curated act.
3. **`artifacts.py`** — the registry of durable outputs (commits, diffs,
   reports, logs, JSON results). Workers communicate through artifacts; the
   supervisor passes *references*, not transcript copies.
4. **`context/`** — the context compiler. Builds the smallest high-signal packet
   a job needs, then freezes it to disk as the job's `context.md` / `prompt.md`.
5. **`planner/`** — goal → plan DAG via workflow templates (feature, bug,
   refactor, research, experiment, migration), risk classification and review
   routing.
6. **`policy/`** — immutable safety invariants (cannot be self-modified),
   learnable orchestration policy (versioned, reviewable), and **project policy
   packs** that hold project-specific rules outside the generic core.
7. **`providers/`** — agent runtime abstraction. A deterministic mock provider
   proves the whole system with zero paid calls; a Claude Code adapter runs real
   work. `providers/discovery.py` resolves the concrete model id and effort
   vocabulary from the local installation rather than hardcoding either.
8. **`policy/routing.py`** — which model and how much thinking each role gets.
   Policy, not orchestration logic: the scheduler asks and complies. Critical
   roles have an immutable floor. See [model routing](model-routing.md).
9. **`experiments.py` / `delegation.py`** — the pair lock that keeps A/B arms
   comparable, and the rule that only the supervisor creates jobs.
10. **`scheduler.py` / `supervisor.py`** — the outer loop: select ready jobs,
   lease them, dispatch, ingest results, route to review, revise or land,
   verify, evaluate goal completion, update memory and metrics, repeat.

## Project policy packs

The core knows nothing about any particular project. A pack is a small
declarative module that supplies: repo identity rules, protected paths, risk
overrides, extra immutable rules, required verification commands, and
human-gate triggers. The repository ships one worked example,
`devsupervisor/policy/packs/example.py` (exact-SHA landing, frozen benchmark
isolation, harness-vs-product commit identity, paid-experiment gates). A real
project's pack lives outside the repository, in `$DEVSUPERVISOR_HOME/packs/`,
because it names that project — the registry loads every `*.py` there by the
same `@register` mechanism.

## Why each piece of scaffolding exists

Scaffolding is a liability: it must earn its place, and it must be removable as
models get stronger. Each component records its justification.

| Component            | Exists because                                                       | Removable when |
|----------------------|----------------------------------------------------------------------|----------------|
| Model routing floor  | a cheaper reviewer does not save money, it changes what "approved" means | never |
| Experiment pair lock | any difference besides the treatment is an alternative explanation   | never          |
| Supervisor-only spawn| a worker that spawns can review itself and fan out without a budget   | never          |
| SQLite state         | context windows die; work must not be re-done                        | never          |
| Job state machine    | "approved" must mean one thing, checkable                            | never          |
| Leases               | two agents silently solving one job corrupts results                 | never          |
| Context compiler     | dumping history into every worker is expensive and lowers signal      | when context is free and attention is perfect |
| Independent reviewer | a builder grading itself is not evidence                             | when self-assessment is calibrated |
| Contract-before-build| review rejections cluster on unstated scope                          | when planners state scope reliably |
| Memory candidates    | a worker's claim is a hypothesis, not a fact                         | when workers are reliably calibrated |
| Bounded retries      | loops burn money without producing evidence                          | never          |
| Human gates          | some actions are irreversible                                        | never          |

## Data flow for one unit of work

```
goal ──planner──► plan DAG ──scheduler──► READY job
   │                                          │ lease
   │                            context compiler builds packet
   │                                          ▼
   │                                     provider runs worker
   │                                          │ writes artifacts
   │                                   structured result ingested
   │                                          ▼
   │                            review policy → reviewer job (fresh context)
   │                                    ├── REJECT → targeted revision job
   │                                    └── APPROVE → landing job (exact SHA)
   │                                                     ▼
   └───────────────────── evaluator: did this satisfy the goal? ────────► DONE
```
