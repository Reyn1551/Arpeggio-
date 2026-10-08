-- Worktrees and patch mode (M0.4): the commit an attempt's worktree branch started from, and
-- a machine-readable reason when an attempt fails before verification (for example
-- patch_unsafe). The full detail of a failure lives in a step and its artifact.
-- See docs/04-DATA-MODEL.md.

ALTER TABLE attempts ADD COLUMN base_sha       TEXT;
ALTER TABLE attempts ADD COLUMN failure_reason TEXT;
