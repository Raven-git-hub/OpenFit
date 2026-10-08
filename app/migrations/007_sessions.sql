-- Sessions: interval records beside the daily metrics.
--
-- A metric is one number per (date, source); some things are an interval
-- with a shape instead - a night's sleep with its stage breakdown, later
-- a workout. sessions holds one row per such interval: `kind` says what
-- it is ('sleep'), `start`/`end` are ISO 8601 UTC timestamps, and
-- `summary_json` carries the kind's breakdown as JSON, so a new summary
-- field needs no migration.
--
-- `id` is source:kind:start (built by write_session() in
-- plugins/base.py), so a re-sync of the same night replaces its row
-- rather than adding a second one.
--
-- Sleep is dual-track: the daily sleep_minutes metric is written exactly
-- as before, and the session sits beside it.
--
-- Purely additive: one new table and its index, nothing existing is
-- read, altered or dropped, so this is safe to apply to a populated
-- production database. IF NOT EXISTS means a database that somehow
-- already has a sessions table adopts this migration without error.
--
-- `start` and `end` are quoted everywhere: END is an SQL keyword.

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    "start" TEXT NOT NULL,
    "end" TEXT,
    summary_json TEXT,
    synced_at TEXT
);

-- Sessions are read by kind over a time range.
CREATE INDEX IF NOT EXISTS idx_sessions_kind_start ON sessions (kind, "start");
