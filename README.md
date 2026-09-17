# DevSupervisor

Autonomous software development supervisor that plans, delegates, reviews,
verifies, and lands engineering work across projects.

It turns a high-level engineering goal into a durable plan, delegates the work to
disposable specialist agents, reviews their output with an independent reviewer,
handles targeted revisions, lands approved work, verifies the outcome, and
continues across restarts until the goal is done or a human decision is required.

**The conversation is not the source of truth.** The model is a reasoning engine
inside a deterministic harness. State lives in SQLite, memory lives in small
path-addressed documents, and work moves between agents as artifacts.

## Architecture

```
┌──────────────────────────────────────────────┐
│  DETERMINISTIC HARNESS (Python, no model)    │
│  state machine · scheduler · leases · gates  │
│  policy · persistence · metrics · CLI        │
└───────────────┬──────────────────────────────┘
                │ compiled job packets  ▲ structured results
                ▼                       │
┌──────────────────────────────────────────────┐
│  PROVIDER (mock | claude-cli)                │
│  disposable agent processes                  │
└──────────────────────────────────────────────┘
```

Deterministic code owns anything where a wrong answer is a correctness bug: job
state transitions, dependency readiness, retry bounds, lease acquisition,
reviewer independence, human-gate evaluation, landing preconditions (SHA
identity), secret redaction, and cost accounting. The model is used for
judgment: decomposing a goal, planning inside a job, writing the code or the
review, and deciding which memory is relevant.

```
devsupervisor/
  state/      SQLite schema, state machine, store, leases
  memory/     path-addressed docs, redaction, candidates, curation
  context/    the job-packet compiler
  planner/    goal → plan DAG, workflow templates, risk
  policy/     immutable rules, learnable policy, project packs
  providers/  agent runtime abstraction (mock, claude-cli)
  scheduler.py supervisor.py metrics.py retrospective.py cli.py
docs/         architecture and the frozen spec
tests/        the acceptance suite (stdlib unittest, no network)
examples/     a sample project fixture
```

## Install

No packaging step and no third-party dependencies — Python 3 standard library
only. Run it straight from a checkout:

```bash
git clone git@github.com:nTony46/DevSupervisor.git
cd DevSupervisor
export PATH="$PWD/bin:$PATH"
devsup doctor
```

`devsup doctor` reports the runtime root, database schema version, supervisor
lock, configured providers, model routing, open human gates, and stale leases.

## Simplest way to run it

```bash
devsup init ~/path/to/repo --name myproject
devsup goal add myproject "Build feature X"
devsup plan <goal-id>
devsup run <goal-id> --dry-run
devsup status
```

Nothing costs money by default: the default provider is a deterministic mock.
A paid provider has to be asked for explicitly — `--provider claude-cli
--allow-paid --budget <dollars>` — so no default path, test, or accidental
invocation can spend money.

Useful commands: `devsup jobs`, `devsup job show <id>`, `devsup gates`,
`devsup approve|reject <gate-id>`, `devsup pause`, `devsup resume`,
`devsup runs <job-id>`, `devsup memory list|curate|adopt`, `devsup retrospect`,
`devsup routing`, `devsup permissions`, `devsup import-handoffs`.

## Dashboard

A read-only operator view of what the supervisor is doing right now:

```bash
devsup dashboard          # or: python3 -m devsupervisor.dashboard
```

then open `http://127.0.0.1:8765` (`--port` to change it). It binds to loopback
only and refuses every HTTP method that is not a read; the database is opened
`mode=ro` with `query_only`, so no query it runs can change a row. (SQLite still
creates the usual `-wal`/`-shm` sidecars next to the database, so the runtime
root must be writable even though no supervisor data is altered.)

The page shows the current pipeline, a live agent graph (supervisor at the
centre, active workers lit, idle role capacity greyed out), and a chronological
activity log, plus branch/SHA, active lease count and spend. An open human gate
takes over the page — it reports the decision needed, and you still answer it
through `devsup approve|reject`.

Everything is derived from durable state on each request — job transitions, gate
rows, runs and landing events — and the dashboard adds no tables of its own, so
history survives closing the tab, stopping the server, or rebooting. A project
selector appears when more than one project is registered; each project's goal,
pipeline, agents, gates, branch and spend are reported independently.

## How projects are represented

A project is a registered repository. `devsup init <repo>` records its name,
resolved git toplevel, and optional policy pack, then creates its runtime
directories. A git repo is expected — SHA-based review and landing cannot work
otherwise — and registering anything else requires `--allow-non-git`. Goals
belong to a project; planning turns a goal into a dependency graph of jobs; jobs
are leased, dispatched to a provider, reviewed, and landed.

## Runtime and durable state

Nothing durable lives in this repository. Everything is written under
`$DEVSUPERVISOR_HOME` (default `~/.devsupervisor`):

```
$DEVSUPERVISOR_HOME/
  supervisor.db                  SQLite (WAL, foreign keys, every transition logged)
  logs/
  projects/<project-id>/
    memory/<area>/               product, architecture, decisions, conventions,
                                 experiments, incidents, lessons, orchestration
    memory-candidates/
    jobs/<job-id>/
    transcripts/
    artifacts/
    checkpoints/
```

Pointing `DEVSUPERVISOR_HOME` at a temp directory gives a fully isolated
instance; that is exactly what the test suite does.

## Tests

```bash
./scripts/test.sh
```

371 tests, stdlib `unittest`, no network and no paid calls (`tests/test_no_paid_calls.py`
asserts the latter). Every test points `DEVSUPERVISOR_HOME` at a temp directory,
so the suite never touches a real runtime root.

## Current limitations

- Local and single-machine. One supervisor holds a lock at a time; the only
  bundled UI is the read-only localhost dashboard.
- The dashboard shows at most 12 agent nodes at once, declaring the remainder
  as a count rather than drawing them.
- Two providers: a deterministic `mock` (default, free) and `claude-cli`, which
  shells out to the `claude` CLI and must be on `PATH`.
- Run from a checkout. There is no `pip install`, no entry point beyond
  `bin/devsup`, and no versioned release.
- Landing is git-based and expects a clean, SHA-identifiable tree.
- The planner works from workflow templates, not arbitrary open-ended decomposition.
- macOS/Linux oriented; not tested on Windows.

## Docs

- [architecture](docs/architecture.md)
- [state model](docs/state-model.md)
- [agent loops](docs/agent-loop.md)
- [context and memory](docs/context-and-memory.md)
- [execution policy](docs/execution-policy.md)
- [model routing](docs/model-routing.md)
- [safety and human gates](docs/security-and-human-gates.md)
- [acceptance tests](docs/acceptance-tests.md)
- [handoff reconciliation](docs/handoff-reconciliation.md)
