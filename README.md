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

**Project rules live in a policy pack**, not in the core: protected paths, a
required verification command, extra hard rules, risk floors, and the subjects
that must open a human gate. `devsupervisor/policy/packs/example.py` is a
complete worked example. Copy it to `~/.devsupervisor/packs/<project>.py`,
rename it, edit the rules, and pass `--pack <name>` to `devsup init`. Packs in
that directory load by name exactly like the shipped one, and never need to be
committed to this repository.

## Working with Claude

There are two ways to put a real model behind the supervisor. Both use the
same durable state, so you can mix them and the dashboard shows either.

### 1. Headless: the supervisor drives Claude Code

```bash
devsup run <goal-id> --provider claude-cli --allow-paid --budget 20
```

The supervisor compiles a job packet for each ready job, runs `claude -p` on
it in a disposable worktree, parses the structured result block the worker is
told to end with, and moves the job through the state machine. The budget is a
hard cap that survives restarts; a missing result block is a failed run, not a
guess. This is the fully autonomous mode and it is the one the test suite
exercises (with the mock provider standing in for `claude`).

### 2. Interactive: a Claude Code session acts as the supervisor

This is how the project's own author uses it day to day. Open Claude Code in
the project and paste a structured brief. Claude keeps the durable state in
DevSupervisor, spawns disposable subagents for the roles below, and reports
back when the work has landed or a decision is needed.

A brief that works well says four things: the goal, the constraints, what
"done" means, and when to stop and ask. For example:

```
# myproject — identifier-miss fallback

Goal: when a task description names an identifier that does not exist in the
index, the retrieval packet must fall back to prose search instead of coming
back empty.

Constraints: do not change the ranking of hits that already work. No force
push, no history rewrite. Land on main only after an independent review.

Done means: a regression test that fails on main today and passes after; the
existing suite green; the change measured on the 75-case set with the numbers
in the commit message.

Stop and open a gate if: the fix needs a threshold, and you would have to
guess the value. When done, STOP and report the final SHA.
```

The session then runs the outer loop by hand: register the goal (`devsup goal
add`), plan it, hand the build to a worker agent in its own clone, hand the
result to a *different* reviewer agent that never saw the builder's reasoning,
loop on REJECT with numbered blockers, land the exact approved SHA, and run
`devsup approve|reject` decisions past you as gates rather than deciding them
itself. Prompts are pasted; conversation history is not the record — `devsup
status`, `devsup jobs`, and the dashboard are.

### The agents

Every job carries a role. A role sets what the worker may touch and how much
the harness trusts its word.

| Role | What it does | Trust |
|---|---|---|
| `investigator` | reads the current system, reproduces a bug, captures a baseline; changes nothing | disposable |
| `planner` | turns a goal into a contract: scope, non-goals, what done means | privileged |
| `architect` | writes the design, including the alternatives it rejected | disposable |
| `build` | implements against the contract in its own worktree; returns a SHA and evidence | disposable |
| `reviewer` | independent APPROVE/REJECT with numbered blockers; may not fix what it reviews | disposable, critical |
| `specialist` / `security` | risk-specific review for what the change touches | disposable, critical |
| `qa` | proves behaviour is unchanged against a captured baseline | disposable |
| `landing` | checks the approved SHA still exists, lands it with safe git, runs verification | privileged |
| `evaluator` | asks whether the landed change satisfies the *goal*, not whether the code is nice | disposable, critical |
| `researcher` | gathers evidence and analyses results, including negative ones | disposable |
| `operator` | runs an experiment and captures the raw output | privileged |
| `supervisor` | the outer loop itself | privileged, critical |

*Disposable* roles run unattended with edit permission in a throwaway
worktree; nothing they do reaches shared state except through a later,
separately authorised step. *Privileged* roles act on shared state and are
bounded by deterministic checks, never by a model's judgment. *Critical* roles
have a model-routing floor that a learnable policy may raise but not lower.

Three rules are enforced by the state machine, not by convention: the agent
that did the work cannot approve it; a job whose review policy is not `none`
cannot be approved without going through review; and a job that produced a
landable candidate cannot close at `APPROVED` — it goes through landing and
verification or it does not finish.

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

## License

MIT — see [LICENSE](LICENSE).
