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

## Quick start — with Claude Code

Python 3 standard library only. No install step, no dependencies.

```bash
git clone git@github.com:nTony46/DevSupervisor.git
export PATH="$PWD/DevSupervisor/bin:$PATH"     # put this in your shell profile
devsup doctor                                  # checks python, git, and `claude`

devsup init ~/path/to/your-repo --name myproject
cp DevSupervisor/templates/CLAUDE.md ~/path/to/your-repo/CLAUDE.md   # or append to an existing one
```

Then open Claude Code in your repo and give it a task the way you normally
would. The `CLAUDE.md` tells the session that work here goes through
DevSupervisor: it registers your task as a goal, plans it, runs the loop with
the budget you name, and brings every human decision back to you as a gate.

**Do you have to say "use DevSupervisor" in each prompt?** Not with the
`CLAUDE.md` in place — that file is the instruction, loaded at the start of
every session. Without it, Claude Code has no way to know the tool exists and
will simply do the work itself. (After a few sessions Claude Code's own memory
for a directory tends to pick the habit up anyway, but the file makes it
deterministic from the first prompt.)

**Nothing costs money by default.** The default provider is a deterministic
mock, and `CLAUDE.md` tells the session never to add `--allow-paid` unless you
say the work may spend. A real run looks like:

```
Add prose-search fallback for identifiers that miss the index. Budget $20.
Stop and ask if the fix needs a threshold you would have to guess.
```

A good task says four things: the goal, the constraints, what "done" means,
and when to stop and ask. What comes back is a landed SHA, what was verified,
and what it cost — from durable state, not from the chat.

### What the session runs for you

```bash
devsup goal add myproject "…" --criteria "…"     # the task, as a row
devsup plan <goal-id>                              # goal → job graph
devsup run myproject --dry-run                     # what would dispatch
devsup run myproject --provider claude-cli --allow-paid --budget 20
devsup gates                                       # decisions waiting on you
devsup approve <gate-id> --actor you               # only ever after you answer
devsup status
```

You can run any of these yourself, too. Other commands you'll reach for:
`devsup jobs`, `devsup job show <id>`, `devsup reject <gate-id>`,
`devsup pause|resume`, `devsup memory list|curate|adopt`, `devsup retrospect`.

### Project rules: policy packs

Rules for a specific repository — protected paths, a required verification
command, extra hard rules, risk floors, and the subjects that must open a
human gate — live in a policy pack, not in the core.
`devsupervisor/policy/packs/example.py` is a complete worked example. Copy it
to `~/.devsupervisor/packs/<project>.py`, rename it, edit the rules, and pass
`--pack <name>` to `devsup init`. Packs in that directory load by name exactly
like the shipped one and never need to be committed to this repository.

## How the loop runs

`devsup run` compiles a job packet for each ready job, runs `claude -p` on it
in a disposable worktree, parses the structured result block the worker is
told to end with, and moves the job through the state machine. The budget is a
hard cap that survives restarts; a missing result block is a failed run, not a
guess. A Claude Code session with the `CLAUDE.md` above is the *operator* of
this loop — it adds goals, plans, runs, and relays gates — while the workers
are separate `claude -p` processes that never see the operator's conversation.

The same loop runs unattended without a Claude Code session in front of it:
`devsup resume` continues every project from durable state, and the test suite
drives it end to end with the mock provider standing in for `claude`.

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
templates/     the CLAUDE.md to drop into a supervised repository
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
[bootstrap log](docs/BOOTSTRAP_LOG.md)

## License

MIT — see [LICENSE](LICENSE).
