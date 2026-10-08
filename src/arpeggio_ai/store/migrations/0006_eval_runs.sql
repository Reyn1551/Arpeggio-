-- Baseline experiment (M0.6): run status and budget fields on eval_runs, and one
-- eval_results row per task and repeat. Status values are checked in Python (Literal
-- types in store/repositories.py), not with SQL CHECK constraints.
-- eval_results is rebuilt because its primary key gains repeat_index and task_id becomes
-- nullable (a skipped task has no task row). Nothing references eval_results, so the
-- rebuild needs no foreign key changes. See docs/04-DATA-MODEL.md.

ALTER TABLE eval_runs ADD COLUMN status        TEXT    NOT NULL DEFAULT 'completed';
ALTER TABLE eval_runs ADD COLUMN status_reason TEXT;
ALTER TABLE eval_runs ADD COLUMN repeats       INTEGER NOT NULL DEFAULT 1;
ALTER TABLE eval_runs ADD COLUMN planned       INTEGER;
ALTER TABLE eval_runs ADD COLUMN estimate_usd  REAL;

CREATE TABLE eval_results_new (
    eval_run_id         TEXT    NOT NULL REFERENCES eval_runs(id),
    eval_task           TEXT    NOT NULL,
    task_id             TEXT    REFERENCES tasks(id),
    solved              INTEGER NOT NULL,
    cost_usd            REAL    NOT NULL,
    attempts            INTEGER NOT NULL,
    duration_s          REAL    NOT NULL,
    quota_wait_s        REAL    NOT NULL DEFAULT 0,
    repeat_index        INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'completed',
    status_reason       TEXT,
    escalations         INTEGER NOT NULL DEFAULT 0,
    estimate_usd        REAL,
    counterfactual_usd  REAL,
    tags                TEXT,
    PRIMARY KEY (eval_run_id, eval_task, repeat_index)
);

INSERT INTO eval_results_new (eval_run_id, eval_task, task_id, solved, cost_usd, attempts,
    duration_s, quota_wait_s, status)
SELECT eval_run_id, eval_task, task_id, solved, cost_usd, attempts, duration_s, quota_wait_s,
    CASE WHEN solved THEN 'solved' ELSE 'failed' END
FROM eval_results;

DROP TABLE eval_results;
ALTER TABLE eval_results_new RENAME TO eval_results;
