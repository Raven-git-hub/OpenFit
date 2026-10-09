-- Derived metrics: the one trusted value per (date, metric), stored.
--
-- metrics keeps every source's reading; when two sources report the same
-- metric for the same day, one of them has to be picked. That pick used
-- to happen on the fly in /api/activity on every request. derived_metrics
-- holds its result instead - one row per (date, metric), carrying the
-- winning reading's value, unit and synced_at, with `source` naming the
-- source it came from (the provenance) - so the pick is made once, in
-- app/derived.py, and everything reads the stored value.
--
-- Rows are written by recompute_derived() (app/derived.py), never by a
-- plugin: after each sync for the window it touched, and once on first
-- boot to backfill a database that predates this table. This file only
-- creates the table - it reads nothing, so it is empty until then.
--
-- Purely additive: one new table and its index, nothing existing is
-- read, altered or dropped, so this is safe to apply to a populated
-- production database. IF NOT EXISTS means a database that somehow
-- already has a derived_metrics table adopts this migration without
-- error.

CREATE TABLE IF NOT EXISTS derived_metrics (
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source TEXT NOT NULL,
    synced_at TEXT,
    PRIMARY KEY (date, metric)
);

-- Derived values are read by metric over a date range.
CREATE INDEX IF NOT EXISTS idx_derived_metric_date ON derived_metrics (metric, date);
