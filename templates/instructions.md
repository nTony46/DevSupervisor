# DevSupervisor

This repository is supervised by DevSupervisor (`devsup` on PATH). Engineering
work here is planned, delegated, reviewed, and landed through it, not done
directly in this session. The conversation is not the record; `devsup status`,
`devsup jobs`, and the dashboard are.

## When the user gives you a goal

1. Register it, plan it, and show the plan before spending anything:

   ```bash
   devsup goal add <project> "<goal title>" --criteria "<what done means>" ...
   devsup plan <goal-id>            # add --risk HIGH|CRITICAL if the subject warrants it
   devsup run <project> --dry-run   # what would dispatch, and which gates are open
   ```

2. Run it with the budget the user gave. Never invent a budget, and never add
   `--allow-paid` unless the user said the work may spend money:

   ```bash
   devsup run <project> --provider claude-cli --allow-paid --budget <usd>
   ```

   Workers run in their own worktrees; an independent reviewer checks each
   result; landing uses the exact approved SHA. Let the loop do this. Do not
   implement, review, or land the work yourself in this session.

3. When `devsup run` stops on a gate, bring the decision to the user verbatim
   (`devsup gates`), with your recommendation. **Only the user approves or
   rejects** — run `devsup approve|reject <gate-id> --actor <user>` after they
   answer, never on your own judgment. Then `devsup run <project> ...` again.

4. Report from durable state when a goal finishes: the landed SHA, what was
   verified, and the spend (`devsup status`, `devsup job show <id>`).

## Rules

- Never force push, rewrite history, or bypass a review or a gate.
- If a `devsup` command fails, show the error; do not fall back to doing the
  work by hand.
- Small questions ("what is the status?", "what is blocked?") are answered from
  `devsup status` / `devsup jobs` / `devsup gates`, not from memory of the chat.
- Project rules live in the project's policy pack, not here. Do not restate or
  reinterpret them.
