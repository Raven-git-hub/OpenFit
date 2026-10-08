-- Activity: one wide row per (date, source) -> one tidy row per reading.
--
-- The activity table had a column per metric (steps, resting_hr,
-- sleep_hours), so every new metric meant a schema change. metrics holds
-- one row per (date, source, metric) instead: a new metric is a new key
-- in the canonical vocabulary (app/metrics.py), not a migration.
--
-- Each source still owns its own rows, exactly as under 002 - the key
-- just gains the metric. And because every metric is its own row, a
-- partial sync only touches the readings it has and can't blank the
-- others, so writers no longer need to COALESCE.
--
-- Existing activity rows are unpivoted into the canonical keys and units,
-- then activity is dropped. The runner wraps this whole file in one
-- transaction, so it either lands completely or not at all.

CREATE TABLE metrics (
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    synced_at TEXT,
    PRIMARY KEY (date, source, metric)
);

-- One INSERT per old column. NULLs are skipped rather than carried over:
-- a missing reading is simply no row, never an empty one. date, source
-- and synced_at come across unchanged - 002 already turned a missing
-- source into 'unknown'.
INSERT INTO metrics (date, source, metric, value, unit, synced_at)
SELECT date, source, 'steps', steps, 'count', synced_at
FROM activity
WHERE steps IS NOT NULL;

INSERT INTO metrics (date, source, metric, value, unit, synced_at)
SELECT date, source, 'resting_hr_bpm', resting_hr, 'bpm', synced_at
FROM activity
WHERE resting_hr IS NOT NULL;

-- Sleep is stored in whole minutes, the unit both devices report in.
-- The old hours were rounded to one decimal place, which is always a
-- whole number of minutes, so this loses nothing.
INSERT INTO metrics (date, source, metric, value, unit, synced_at)
SELECT date, source, 'sleep_minutes', ROUND(sleep_hours * 60), 'min', synced_at
FROM activity
WHERE sleep_hours IS NOT NULL;

DROP TABLE activity;

-- The API reads recent readings by date across all sources.
CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics (date);
