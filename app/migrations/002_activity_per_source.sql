-- Activity: one row per date -> one row per (date, source).
--
-- The old activity table had date as the sole PRIMARY KEY, so Garmin and
-- Google Health contended for the same row: whichever synced last owned
-- the day, and the `source` column recorded only the most recent writer.
-- Each source now owns its own row and nothing is overwritten across
-- sources.
--
-- SQLite cannot alter a primary key in place, hence the standard
-- create-new / INSERT..SELECT / drop-old / rename dance. The runner wraps
-- this whole file in one transaction, so it either lands completely or
-- not at all.

CREATE TABLE activity_new (
    date TEXT NOT NULL,
    steps INTEGER,
    resting_hr INTEGER,
    sleep_hours REAL,
    source TEXT NOT NULL,
    synced_at TEXT,
    PRIMARY KEY (date, source)
);

-- Existing rows migrate 1:1 - they already carry a single source. Rows
-- written before the source column was populated (NULL or empty) are
-- kept under 'unknown' rather than dropped: partial provenance beats
-- silent data loss.
INSERT INTO activity_new (date, steps, resting_hr, sleep_hours, source, synced_at)
SELECT
    date,
    steps,
    resting_hr,
    sleep_hours,
    CASE
        WHEN source IS NULL OR TRIM(source) = '' THEN 'unknown'
        ELSE source
    END,
    synced_at
FROM activity;

DROP TABLE activity;

ALTER TABLE activity_new RENAME TO activity;

-- The API reads recent activity by date across all sources.
CREATE INDEX IF NOT EXISTS idx_activity_date ON activity (date);
