-- DEFERRABLE is an SQLite keyword, so every query had to quote tasks."deferrable".
-- Rename it once so raw SQL and views can use a plain identifier. Position, type and
-- default stay the same. See docs/04-DATA-MODEL.md.

ALTER TABLE tasks RENAME COLUMN "deferrable" TO is_deferrable;
