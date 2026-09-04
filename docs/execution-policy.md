# Execution Policy — permission bypass, and what it is not

## The distinction this rests on

Permission bypass is **autonomy inside a disposable sandbox**. It is never
**authority**.

A bypassed worker can run whatever it needs to do its job without stopping to
ask a human who is not there. It still cannot land, push, rewrite history,
mutate a benchmark, or spend outside budget — those are decided by DevSupervisor
before and after the worker runs, and the worker's tool permissions have no
bearing on any of them.

The two are easy to conflate, which is exactly why they are separated in code: a
process that can do anything in its sandbox looks, from the inside, identical to
a process that has been authorised to do anything.

## Default policy

| Role | Mode | Why |
|---|---|---|
| build, reviewer, specialist, security, researcher, investigator, benchmark, evaluator, qa, architect | `bypassPermissions` | disposable, works in its own worktree, produces an artifact, is thrown away |
| landing, operator, freeze, supervisor, planner | `dontAsk` | acts on shared state; bounded by a contract, not a preference |

Resolved per role, persisted on the job, and recorded on every run.

## Two independent refusals

1. **A privileged role never runs bypassed**, whatever a policy says. That is a
   policy error and raises.
2. **A bypassed worker never runs in a shared checkout.** Every registered
   project's own `repo_path` counts as shared. Missing isolation is an
   environment fact rather than a mistake, so it *downgrades* to guarded — but
   never silently: the downgrade is written as a `permission.downgraded` event
   with its reason.

## Command construction

Verified against the installed CLI, which validates `--permission-mode` against
its own choice list at parse time (so syntax is checkable for free):

```
# builder — isolated, disposable, autonomous
claude -p <packet> --output-format json \
  --model claude-opus-5 --effort high --max-budget-usd 15.0 \
  --allowedTools Read Grep Glob Bash Edit Write MultiEdit NotebookEdit \
  --permission-mode bypassPermissions --dangerously-skip-permissions \
  --permission-prompts none            # cwd: a disposable worktree

# lander — privileged, guarded, bounded by contract
claude -p <packet> --output-format json \
  --model claude-opus-5 --effort high --max-budget-usd 15.0 \
  --allowedTools Read Grep Glob Bash \
  --permission-mode dontAsk \
  --permission-prompts none            # cwd: the shared checkout
```

Both bypass forms are emitted together: the mode names the intent in a value the
CLI itself validates, and `--dangerously-skip-permissions` is the flag its help
text documents for this.

## Landing is bounded by a contract, not by tools

Because a lander cannot be made safe by restricting its shell, it is made safe
by checking the world before and after it runs.

**Before dispatch** (`landing.preconditions`): the target must be an approved
candidate, an approval must be recorded by someone who did not do the work, the
approved SHA must exist in the repository, and the target ref is resolved *at
dispatch time* and stored — never assumed from the plan.

**After the run** (`landing.verify`) — three checks, because a landing fails in
three ways that all look like success from inside the worker:

1. the approved SHA is actually in the target now;
2. the target's previous tip is still an ancestor of it — this is what makes
   "never force push" checkable rather than merely instructed;
3. the patch that landed is the patch that was reviewed.

If no target ref resolves, the result is reported as **unverified**, not as
verified. `landing.verified` is recorded either way, with the reason.

## Everything else bypass does not change

- **Pair lock.** `permission_mode` is a locked field: two arms that ask
  differently are not two arms of one experiment. Dispatch locks the mode across
  every arm before either runs.
- **Delegation.** A subtask request may not set `permission_mode`, `model`,
  `effort`, `tools`, `provider`, or `max_budget_usd` — asking for a child to run
  bypassed is asking to escalate. The supervisor resolves execution policy from
  role policy at dispatch, after it has authorised the child.
- **Read-only integrity.** Read-only roles still have their worktree snapshotted
  before the run and asserted byte-identical afterwards. Bypass does not exempt
  a reviewer from being a reviewer.
- **Immutable command checks.** Force-push and history-rewrite shapes are
  refused by `immutable.check_command` regardless of permission mode.

## Recorded per run

`role · permission_mode · bypass_permissions · worktree · provider · model asked
for · model that answered · effort · tools · budget · tokens · cost · duration ·
attempt · review outcome · permission denials · turns`

`devsup permissions` prints the policy; `devsup runs <job-id>` prints the record.
