# Safety Invariants and Human Gates

Two kinds of policy exist and they are stored differently.

- **Immutable policy** — hard-coded, not in the database, not writable by any
  retrospective, worker, or LLM. Violations raise and abort the action.
- **Learnable policy** — orchestration heuristics, versioned rows in
  `policies`, changed only by an explicit adoption of a reviewed candidate.

The self-improvement loop can only ever produce candidates against the second.
`immutable.py` exposes no mutation API at all — this is the enforcement.

## Immutable rules

1. Never force push; never rewrite shared history destructively.
2. Never silently mutate a frozen benchmark or evaluation package.
3. Never leak private oracle / held-out data into an implementation worker.
4. Never store `.env` contents, credentials, tokens, private keys, or secrets in
   shared memory or a job packet.
5. Never use an eval/harness commit as if it were a product SHA.
6. Never land code that requires review without a recorded approval.
7. A builder may never be the independent reviewer of its own work.
8. Never start substantial paid model/agent runs without a configured budget and
   an approval.
9. Stop on ambiguous repository identity rather than guessing.
10. Require explicit approval for irreversible or destructive actions.
11. Never route a critical reasoning role below the strongest available Opus
    model at high effort, and never give one a fallback model.
12. Both arms of a paired experiment run identically except for the treatment
    under test.
13. Only the supervisor creates jobs. A worker may request a subtask; it may not
    spawn one.

Rules 11–13 are detailed in [model routing](model-routing.md).

## Risk levels and review routing

| Risk | Examples | Required chain |
|---|---|---|
| LOW | internal docs, comments | focused self-verification; reviewer optional |
| MEDIUM | ordinary product code | independent code reviewer |
| HIGH | auth, security, schema, data-loss, performance-sensitive | independent reviewer **+** specialist review/QA |
| CRITICAL | irreversible, destructive, security boundary, frozen-eval integrity | human gate **+** specialist review |

The builder may self-test. It may not self-approve when policy requires
independent review — attempting it raises `ReviewerIndependenceViolation`.

## Review protocol

The reviewer receives a clean scoped packet naming the exact candidate SHA or
artifact. It does not silently fix what it is reviewing; it returns:

```
APPROVE
```
or
```
REJECT
Blockers:
1. ...
2. ...
```

A rejection creates a **targeted revision job** carrying only the blockers plus
the context needed to fix them. Default cap: **2** automatic targeted revisions.
Past that, the supervisor diagnoses and replans or escalates — it does not loop.

## Landing

Landing is a separate job with a separate role. It must:

1. verify the approved candidate commit still exists;
2. verify patch identity against what was reviewed;
3. detect target-branch movement since review;
4. use only safe git operations — never force push;
5. invent no unrelated fixes;
6. run the project's required verification;
7. record the final target SHA;
8. return a structured landing report.

## Human-decision gates

A gate pauses the affected work into `WAITING_HUMAN` and is resolved only by an
explicit decision recorded in `human_gates`:

- meaningful product-strategy change;
- significant paid spend;
- benchmark baseline mutation;
- weakening of privacy or security;
- public product/API tradeoff not covered by existing policy;
- destructive or irreversible action;
- unresolved conflicting evidence;
- bounded retries exhausted.

Gates are *not* for questions the repository, the tests, or existing project
policy can already answer. Asking a human something inspectable is a bug.
