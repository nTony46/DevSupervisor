# DevSupervisor

Autonomous software development supervisor that plans, delegates, reviews,
verifies, and lands engineering work across projects.

Give it a goal. It turns that goal into a durable plan, hands the pieces to
disposable specialist agents, has an independent reviewer check their work,
lands what passes, and keeps going across restarts until the goal is done or a
human decision is needed.

**The conversation is not the source of truth.** State lives in SQLite, memory
lives in small path-addressed documents, and work moves between agents as
artifacts. The model reasons; deterministic code decides.

## Quick start

Python 3 standard library only. No install step, no dependencies.

```bash
git clone git@github.com:nTony46/DevSupervisor.git
cd DevSupervisor
export PATH="$PWD/bin:$PATH"

devsup doctor                                   # check the runtime
devsup init ~/path/to/repo --name myproject     # register a git repo
devsup goal add myproject "Build feature X"
devsup plan <goal-id>
devsup run <goal-id> --dry-run
devsup status
```

**Nothing costs money by default.** The default provider is a deterministic
mock. To use a real model you must say so: `--provider claude-cli --allow-paid
--budget <dollars>`.

Other commands you'll reach for: `devsup jobs`, `devsup job show <id>`,
`devsup gates`, `devsup approve|reject <gate-id>`, `devsup pause|resume`,
`devsup memory list|curate|adopt`, `devsup retrospect`.

## Dashboard

A read-only view of what the supervisor is doing right now:

```bash
devsup dashboard        # then open http://127.0.0.1:8765
```

You'll see the current pipeline, a live agent graph (supervisor in the centre,
active workers lit), the activity log, branch/SHA, leases, and spend. When a
human decision is pending, the page says so up front; you answer it with
`devsup approve|reject`.

It binds to loopback only, rejects every non-read HTTP method, and opens the
database read-only. Everything is derived from durable state on each request,
so history survives closing the tab, stopping the server, or rebooting. With
more than one project registered, a selector lets you switch between them.

## How it works

```
┌──────────────────────────────────────────────┐
│  DETERMINISTIC HARNESS (Python, no model)    │
│  state machine · scheduler · leases · gates  │
│  policy · persistence · metrics · CLI        │
└───────────────┬──────────────────────────────┘
                │ job packets          ▲ results
                ▼                      │
┌──────────────────────────────────────────────┐
│  PROVIDER (mock | claude-cli)                │
│  disposable agent processes                  │
└──────────────────────────────────────────────┘
```

Anything where a wrong answer is a bug is deterministic code: state
transitions, retries, leases, reviewer independence, human gates, landing
preconditions, secret redaction, cost accounting. The model handles judgment:
decomposing a goal, writing the code or the review, choosing relevant memory.

<details>
<summary>Repository layout</summary>

```
devsupervisor/
  state/       SQLite schema, state machine, store, leases
  memory/      path-addressed docs, redaction, curation
  context/     the job-packet compiler
  planner/     goal → plan DAG, workflow templates, risk
  policy/      immutable rules, learnable policy, project packs
  providers/   agent runtimes (mock, claude-cli)
  dashboard/   the read-only localhost UI
  scheduler.py supervisor.py metrics.py retrospective.py cli.py
docs/          architecture and the frozen spec
tests/         acceptance suite (stdlib unittest, no network)
examples/      a sample project fixture
```
</details>

## Where state lives

Nothing durable is kept in this repository. Everything goes under
`$DEVSUPERVISOR_HOME` (default `~/.devsupervisor`): the SQLite database, logs,
and per-project memory, jobs, transcripts, artifacts and checkpoints. Point
`DEVSUPERVISOR_HOME` at a temp directory for a fully isolated instance — the
test suite does exactly that.

## Tests

```bash
./scripts/test.sh
```

410 tests, no network, no paid calls (a test asserts it), never touches a real
runtime root.

## Limitations

- Local, single-machine, one supervisor at a time.
- Two providers: `mock` (default, free) and `claude-cli` (needs `claude` on `PATH`).
- Run from a checkout; no `pip install` or versioned release yet.
- Landing is git-based and expects a clean tree.
- The planner works from workflow templates, not open-ended decomposition.
- The dashboard draws at most 12 agents at once and reports the rest as a count.
- macOS/Linux; not tested on Windows.

## Docs

[architecture](docs/architecture.md) ·
[state model](docs/state-model.md) ·
[agent loops](docs/agent-loop.md) ·
[context and memory](docs/context-and-memory.md) ·
[execution policy](docs/execution-policy.md) ·
[model routing](docs/model-routing.md) ·
[safety and human gates](docs/security-and-human-gates.md) ·
[acceptance tests](docs/acceptance-tests.md) ·
[handoff reconciliation](docs/handoff-reconciliation.md)
