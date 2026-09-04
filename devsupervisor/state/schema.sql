-- DevSupervisor durable execution state.
-- Every row here must survive a crash, a context reset, and a laptop restart.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    repo_path   TEXT NOT NULL,
    policy_pack TEXT,
    vcs         TEXT NOT NULL DEFAULT 'git',
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    workflow            TEXT,
    risk                TEXT NOT NULL DEFAULT 'MEDIUM',
    status              TEXT NOT NULL DEFAULT 'OPEN',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id          TEXT PRIMARY KEY,
    goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    workflow    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ACTIVE',
    rationale   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE (goal_id, version)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal_id             TEXT REFERENCES goals(id) ON DELETE CASCADE,
    plan_id             TEXT REFERENCES plans(id) ON DELETE SET NULL,
    job_type            TEXT NOT NULL,
    role                TEXT NOT NULL,
    status              TEXT NOT NULL,
    priority            INTEGER NOT NULL DEFAULT 50,
    risk                TEXT NOT NULL DEFAULT 'MEDIUM',
    repo                TEXT,
    worktree            TEXT,
    base_sha            TEXT,
    branch              TEXT,
    scope               TEXT NOT NULL DEFAULT '',
    non_goals           TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    output_contract     TEXT NOT NULL DEFAULT '',
    review_policy       TEXT NOT NULL DEFAULT 'independent',
    attempt             INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    revision_count      INTEGER NOT NULL DEFAULT 0,
    max_revisions       INTEGER NOT NULL DEFAULT 2,
    revision_of         TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    reviews_job_id      TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    lands_job_id        TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    provider            TEXT,
    model               TEXT,
    session_id          TEXT,
    session_policy      TEXT NOT NULL DEFAULT 'fresh',
    result_sha          TEXT,
    blockers            TEXT NOT NULL DEFAULT '[]',
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_goal    ON jobs(goal_id);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);

CREATE TABLE IF NOT EXISTS job_dependencies (
    job_id             TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    depends_on_job_id  TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, depends_on_job_id)
);

CREATE TABLE IF NOT EXISTS job_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_job ON job_transitions(job_id);

CREATE TABLE IF NOT EXISTS job_relations (
    job_id         TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    related_job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (job_id, related_job_id, kind)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    kind            TEXT NOT NULL,
    project_id      TEXT,
    job_id          TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt         INTEGER NOT NULL,
    role            TEXT NOT NULL DEFAULT '',
    provider        TEXT NOT NULL,
    model           TEXT,
    session_id      TEXT,
    prompt_version  TEXT,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    duration_s      REAL,
    exit_code       INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    transcript_path TEXT,
    result          TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id);

CREATE TABLE IF NOT EXISTS provider_sessions (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    external_id  TEXT,
    job_id       TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    role         TEXT,
    status       TEXT NOT NULL DEFAULT 'OPEN',
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id     TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    kind       TEXT NOT NULL,
    uri        TEXT NOT NULL,
    sha256     TEXT,
    summary    TEXT NOT NULL DEFAULT '',
    metadata   TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);

CREATE TABLE IF NOT EXISTS metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    job_id     TEXT,
    run_id     TEXT,
    name       TEXT NOT NULL,
    value      REAL,
    text_value TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name);

CREATE TABLE IF NOT EXISTS human_gates (
    id            TEXT PRIMARY KEY,
    project_id    TEXT REFERENCES projects(id) ON DELETE CASCADE,
    goal_id       TEXT,
    job_id        TEXT,
    kind          TEXT NOT NULL,
    question      TEXT NOT NULL,
    context       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'OPEN',
    resume_status TEXT,
    decided_by    TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    decided_at    TEXT
);

CREATE TABLE IF NOT EXISTS leases (
    job_id      TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    owner       TEXT NOT NULL,
    token       TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'CANDIDATE',
    rationale  TEXT NOT NULL DEFAULT '',
    evidence   TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    adopted_at TEXT,
    adopted_by TEXT,
    UNIQUE (name, version)
);

-- Single-owner supervisor lock. One row, id = 1.
CREATE TABLE IF NOT EXISTS supervisor_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    owner       TEXT NOT NULL,
    pid         INTEGER,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
