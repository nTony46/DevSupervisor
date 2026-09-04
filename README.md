# DevSupervisor

A local, general-purpose software-development supervisor. It turns a high-level
engineering goal into a durable plan, delegates work to disposable specialist
agents, independently reviews their output, handles targeted revisions, lands
approved work, verifies the outcome, and continues across restarts until the
goal is done or a human decision is required.

**The conversation is not the source of truth.** The model is a reasoning engine
inside a deterministic harness. State lives in SQLite; memory lives in small
path-addressed documents; work moves between agents as artifacts.

## Status

Bootstrap campaign `devsupervisor-v1`. See `.devsupervisor-bootstrap.json` for
the current phase and `docs/BOOTSTRAP_LOG.md` for the history.

## Quick start

```bash
export PATH="$PWD/bin:$PATH"
devsup doctor
devsup init ~/path/to/repo --name myproject
devsup goal add myproject "Build feature X"
devsup plan <goal-id>
devsup run <goal-id> --dry-run
devsup status
```

Nothing costs money by default: the default provider is a deterministic mock.

## Layout

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
examples/     a non-Example sample project fixture
```

## Docs

- [architecture](docs/architecture.md)
- [state model](docs/state-model.md)
- [agent loops](docs/agent-loop.md)
- [context and memory](docs/context-and-memory.md)
- [safety and human gates](docs/security-and-human-gates.md)
- [acceptance tests](docs/acceptance-tests.md)
- [handoff reconciliation](docs/handoff-reconciliation.md)
