# Model Routing, Experiment Pairs, and Who May Spawn

## The opening policy is deliberately expensive

Every reasoning role — supervisor, planner, architect, builder, reviewer,
specialist reviewer, security reviewer, benchmark designer, evaluator,
researcher, investigator, QA, landing, operator — runs on the strongest
available Opus model at high effort.

That includes the mechanical roles. Landing and QA do not obviously need deep
reasoning, and the temptation to route them cheaply is exactly why they are not:
until there are observed outcomes to argue from, a cheaper mechanical role is a
guess. The first campaigns exist to produce the quality baseline that any later
cost reduction has to be measured against.

## Nothing is hardcoded

`providers/discovery.py` resolves the model id and effort level from the machine
it is running on, and records where each answer came from:

| What | Resolved from, in order |
|---|---|
| model id | `DEVSUPERVISOR_OPUS_MODEL` → `~/.claude/settings.json` `modelSettings` key → `settings.model` → `ANTHROPIC_MODEL` → the documented `opus` alias |
| effort | `DEVSUPERVISOR_EFFORT` → `modelSettings[<model>].effortLevel` → `settings.effortLevel` → `CLAUDE_EFFORT` → `high` |
| effort vocabulary | parsed out of `claude --help` |

A configured effort *above* the floor is kept — a machine set to `max` is not
pulled down to `high`. Only a value below the floor is raised, and the raise is
recorded in the resolution notes. If no concrete id is discoverable the alias is
sent and the id the provider reports back is recorded on the run, so the run
record names a real model either way.

`policy/routing.py` contains no model string at all; a test asserts that.

## The floor is immutable

Critical roles — **supervisor, reviewer, specialist, security, evaluator,
benchmark** — cannot be routed below Opus at high effort, and cannot be given a
fallback model. A fallback is a downgrade nobody chose: it answers the question
with a weaker model precisely when the strong one was unavailable.

The floor is enforced twice, on purpose:

- at **proposal** time, so a downgrade never reaches a reviewer looking like an
  ordinary optimisation (`learnable.propose` refuses it for `model_choice`);
- at **use** time, so a policy adopted by any path still cannot take effect.

Non-critical roles may be proposed for cheaper routing through the normal
retrospective mechanism, with observed outcomes as evidence. A candidate has no
effect until someone adopts it by name.

## What every run records

`role · provider · model asked for · model the provider reports · effort ·
routing source · tools · budget · tokens in/out · cost · duration · attempt ·
review outcome`

`devsup runs <job-id>` prints them; `devsup routing` prints the resolved table.

## Experiment pair lock

An A/B is evidence only if the arms differ in exactly one thing. `experiments.py`
locks these across every arm:

`model · effort · risk · repo · base_sha · worktree · branch · scope ·
non_goals · output_contract · review_policy · job_type · timeout · tools ·
max_budget_usd · environment`

The treatment lives in `metadata.experiment.treatment`, deliberately outside the
locked set, because it is the one thing that is supposed to differ.

Routing is *locked across the pair before either arm runs*, rather than stamped
on each arm at its own dispatch. Lazy stamping left the second arm unrouted
while the first was running — which reads as divergence, and worse, would let a
policy change between two dispatches split a pair without anyone noticing. A
hand-edited arm model is corrected to the routed value and the correction is
recorded as an event; routing belongs to policy, not to whoever edited the row.

Divergence in a field the router does not own — a different base commit, a
reworded task, a longer timeout — refuses the dispatch before either arm costs
anything.

## Only the supervisor spawns

A worker returns `subtask_requests` in its structured result. It cannot create a
job, because it has no store handle: the provider contract is a prompt in and a
result out, and `delegation.py` is the only path from a request to a row.

- Workers may request: investigator, researcher, architect, qa, security,
  specialist, reviewer, build. **Not landing** — nothing that writes to a shared
  branch is delegated on a worker's say-so.
- More than four requests in one result opens a human gate instead of fanning out.
- Every authorization records `requested_by` and `authorized_by`.
- The supervisor may fan out up to eight children itself, for A/B arms, parallel
  implementation lanes, research, multiple independent reviewers, or specialist
  QA, and results fan back into the parent goal.
- A requested reviewer is still bound by reviewer independence: it gets a fresh
  session and its approval is checked against the parent's work actors.
