# Context, Memory, and Artifacts

Three different things are routinely confused. They are kept apart here.

| | Lives in | Written by | Read by |
|---|---|---|---|
| **Execution state** | SQLite | harness only | harness |
| **Semantic memory** | small Markdown docs | curator (from candidates) | context compiler |
| **Raw history** | transcripts/, logs/ | providers | humans, retrospectives |
| **Artifacts** | artifacts/ + registry | workers | other workers, by reference |

## Runtime layout

```
$DEVSUPERVISOR_HOME/            (default ~/.devsupervisor)
├── supervisor.db
├── logs/
└── projects/<project-id>/
    ├── STATE.md                 human-readable current state
    ├── ROADMAP.md
    ├── memory/
    │   ├── product/  architecture/  decisions/  conventions/
    │   ├── experiments/  incidents/  lessons/  orchestration/
    ├── memory-candidates/       proposed, never authoritative
    ├── jobs/<job-id>/
    │   ├── spec.md  context.md  prompt.md
    │   ├── report.md  result.json  review.json  metrics.json
    ├── transcripts/
    ├── artifacts/
    └── checkpoints/
```

Memory is **path-addressed and small**: one topic per file. A single growing
`MEMORY.md` becomes unreadable to both humans and the relevance scorer.

## The context compiler

Every worker receives the smallest high-signal packet for *that* job:

```
1. global immutable policy         (safety rules, always, verbatim)
2. project policy pack             (repo rules, protected paths, verification)
3. the user goal                   (why this exists)
4. this job's contract             (scope, non-goals, acceptance, output)
5. exact dependency outputs        (artifact refs from jobs this depends on)
6. relevant durable memory         (scored, capped, path-addressed)
7. verified repo/git facts         (branch, base sha, worktree — read live)
8. previous reviewer findings      (verbatim, only when this is a revision)
```

Explicit non-goals of the compiler:
- it never dumps whole transcripts;
- it never includes memory from another project;
- it never includes a builder's reasoning in a reviewer packet;
- it never includes held-out/oracle data in an implementation packet.

Relevance selection is capped by document count and character budget, and every
included document is cited by path in the packet so a human can audit what the
worker was told.

## Artifacts

Workers communicate through durable artifacts — commits, diffs, reports, test
logs, designs, benchmark manifests, JSON results — registered with a kind, a
path, and a sha256. Downstream jobs receive **references plus a curated
summary**, not a copy of the upstream conversation. This is what lets a fresh
agent continue after a context reset: the artifacts are the handoff.

## Memory candidates and curation

A worker may *propose* memory. It cannot write authoritative memory.

```
worker ──► memory-candidates/<ts>-<slug>.md ──► curator/reviewer ──► memory/<area>/<slug>.md
```

The curation ("sleep") pass reads recent durable memory, reports, and decisions;
finds duplicates, conflicts, and stale material; and writes a **proposed
consolidated version into `memory-candidates/`**. It never overwrites
authoritative memory silently. Adoption is an explicit act with an audit row.

## Secret handling

Nothing that looks like a credential enters shared memory or a job packet.
Import and write paths run a redactor over `.env`-style assignments, private key
blocks, bearer tokens, and high-entropy `KEY=`/`TOKEN=`/`SECRET=` values,
replacing the value with `[REDACTED]`. The redactor is applied at the boundary
(import, candidate write, packet compile), so a bug in one caller cannot leak
past the others.
