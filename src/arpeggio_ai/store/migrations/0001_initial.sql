-- Schema v1, copied from docs/04-DATA-MODEL.md.
-- PRAGMAs (foreign_keys, journal_mode) are set per connection in store/db.py.

CREATE TABLE repos (
    id              TEXT PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    privacy_class   TEXT NOT NULL DEFAULT 'private'
                    CHECK (privacy_class IN ('public','private','client')),
    provider_allow  TEXT,                      -- JSON array; NULL = all configured providers
    created_at      TEXT NOT NULL
);

CREATE TABLE tasks (
    id                  TEXT PRIMARY KEY,
    repo_id             TEXT NOT NULL REFERENCES repos(id),
    parent_id           TEXT REFERENCES tasks(id),
    title               TEXT NOT NULL,
    request             TEXT NOT NULL,          -- original user text
    spec                TEXT,                   -- clarified spec after intake
    category            TEXT,                   -- e.g. 'refactor','feature','bugfix','docs','data_pipeline','ml_experiment'
    risk                TEXT CHECK (risk IN ('low','medium','high')),
    risk_signals        TEXT,                   -- JSON: which signals fired and their weights
    status              TEXT NOT NULL,          -- see lifecycle in 03-ARCHITECTURE
    source              TEXT NOT NULL DEFAULT 'user'
                        CHECK (source IN ('user','eval','shadow')),
    budget_usd          REAL,
    counterfactual_usd  REAL,                   -- estimated cost on default frontier route
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);
CREATE INDEX idx_tasks_status   ON tasks(status);
CREATE INDEX idx_tasks_category ON tasks(category, risk);

CREATE TABLE done_criteria (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    kind        TEXT NOT NULL CHECK (kind IN ('command','file_exists','metric','review')),
    spec        TEXT NOT NULL,                  -- JSON, e.g. {"cmd":"pytest tests/x.py","expect_exit":0}
    origin      TEXT NOT NULL CHECK (origin IN ('user','derived'))
);

CREATE TABLE attempts (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    seq             INTEGER NOT NULL,           -- 1, 2, 3… within the task
    adapter         TEXT NOT NULL,
    model           TEXT NOT NULL,              -- logical id from config
    provider_model  TEXT,                       -- concrete provider model string actually used
    effort          TEXT NOT NULL,
    verification    TEXT NOT NULL CHECK (verification IN ('light','full')),
    route_reason    TEXT NOT NULL,              -- JSON: policy rule id or bandit sample
    mode            TEXT NOT NULL DEFAULT 'normal'
                    CHECK (mode IN ('normal','explore','shadow','tournament')),
    worktree        TEXT,
    branch          TEXT,
    status          TEXT NOT NULL,              -- running|completed|timeout|error|paused|cancelled
    cost_usd        REAL NOT NULL DEFAULT 0,
    cost_estimated  INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    steps_count     INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    checkpoint      TEXT,                       -- JSON for resume
    UNIQUE (task_id, seq)
);
CREATE INDEX idx_attempts_route ON attempts(adapter, model, effort);

CREATE TABLE steps (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT NOT NULL REFERENCES attempts(id),
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,              -- model_call|tool_call|tool_result|message|approval_request
    summary         TEXT,                       -- short human-readable line
    payload_ref     TEXT,                       -- path in artifacts/ for full content
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cached_tokens   INTEGER,
    price_in_per_m  REAL,                       -- USD per 1M input tokens at call time
    price_out_per_m REAL,
    cost_usd        REAL,
    cost_estimated  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE (attempt_id, seq)
);

CREATE TABLE verdicts (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT NOT NULL REFERENCES attempts(id),
    criterion_id    TEXT REFERENCES done_criteria(id),
    kind            TEXT NOT NULL CHECK (kind IN ('check','full_suite','model_review','user_review')),
    passed          INTEGER NOT NULL,
    detail          TEXT,                       -- JSON: exit code, failing tests, review issues
    log_ref         TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0,    -- verification cost counts toward the task
    created_at      TEXT NOT NULL
);

CREATE TABLE approvals (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT REFERENCES attempts(id),
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    action          TEXT NOT NULL,              -- merge|force_push|delete_outside_worktree|migration|deploy|budget_extend|...
    detail          TEXT NOT NULL,              -- JSON: exact command or diff summary
    decision        TEXT CHECK (decision IN ('approved','rejected')),
    decided_by      TEXT,                       -- 'user' (future: other actors)
    requested_at    TEXT NOT NULL,
    decided_at      TEXT
);

CREATE TABLE feedback (
    task_id             TEXT PRIMARY KEY REFERENCES tasks(id),
    accepted_attempt_id TEXT REFERENCES attempts(id),
    post_agent_diff_ref TEXT,                   -- diff between agent output and what was merged
    post_agent_lines    INTEGER,                -- changed lines by user after agent
    interventions       INTEGER NOT NULL DEFAULT 0,
    user_rating         INTEGER CHECK (user_rating BETWEEN 1 AND 5),
    note                TEXT,
    escaped             INTEGER NOT NULL DEFAULT 0,  -- set later if a revert/fix links back
    escape_ref          TEXT,                   -- commit or issue that revealed the problem
    failure_class       TEXT,                   -- reflector output for failed tasks (v2)
    created_at          TEXT NOT NULL
);

CREATE TABLE proposals (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('policy','taste','skill','prompt')),
    title           TEXT NOT NULL,
    rationale       TEXT NOT NULL,              -- which tasks/failures motivated it
    patch_ref       TEXT NOT NULL,              -- path to a git patch
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','gating','accepted','rejected')),
    eval_run_id     TEXT REFERENCES eval_runs(id),
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

CREATE TABLE eval_runs (
    id              TEXT PRIMARY KEY,
    strategy        TEXT NOT NULL,              -- junior|middle|senior|arpeggio|<proposal id>
    split           TEXT NOT NULL CHECK (split IN ('tuning','holdout','all')),
    git_sha         TEXT NOT NULL,              -- harness version under test
    config_hash     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    summary         TEXT                        -- JSON: metrics with confidence intervals
);

CREATE TABLE eval_results (
    eval_run_id     TEXT NOT NULL REFERENCES eval_runs(id),
    eval_task       TEXT NOT NULL,              -- eval task file id
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    solved          INTEGER NOT NULL,
    cost_usd        REAL NOT NULL,
    attempts        INTEGER NOT NULL,
    duration_s      REAL NOT NULL,
    PRIMARY KEY (eval_run_id, eval_task)
);

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
