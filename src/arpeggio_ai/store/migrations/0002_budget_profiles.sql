-- Budget profiles (M0.2.5): profile per task and eval run, deferral, actual-model provenance,
-- price windows and cache-hit prices per step, quota wait time, and free-tier quota usage.
-- 'unknown' only marks rows created before this migration. See docs/04-DATA-MODEL.md.

ALTER TABLE tasks    ADD COLUMN profile        TEXT    NOT NULL DEFAULT 'unknown';
-- DEFERRABLE is an SQLite keyword, so the column name is quoted. It is still `deferrable`.
ALTER TABLE tasks    ADD COLUMN "deferrable"   INTEGER NOT NULL DEFAULT 0;

ALTER TABLE attempts ADD COLUMN model_mismatch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attempts ADD COLUMN deferred_until TEXT;

ALTER TABLE steps    ADD COLUMN actual_model          TEXT;
ALTER TABLE steps    ADD COLUMN price_window          TEXT;               -- 'peak' | 'offpeak' | 'flat'
ALTER TABLE steps    ADD COLUMN price_multiplier      REAL NOT NULL DEFAULT 1.0;
ALTER TABLE steps    ADD COLUMN price_cache_hit_per_m REAL;

ALTER TABLE eval_runs    ADD COLUMN profile      TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE eval_results ADD COLUMN quota_wait_s REAL NOT NULL DEFAULT 0;

CREATE TABLE quota_usage (
    provider        TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    window          TEXT    NOT NULL,          -- 'minute' | 'day'
    window_start    TEXT    NOT NULL,
    requests        INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    remaining_req   INTEGER,
    remaining_tok   INTEGER,
    reset_at        TEXT,
    source          TEXT    NOT NULL,          -- 'config' | 'header'
    PRIMARY KEY (provider, model, window, window_start)
);
