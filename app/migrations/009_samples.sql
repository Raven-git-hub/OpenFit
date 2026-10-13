-- Intraday sample series: timestamped per-source readings (e.g. heart rate
-- every ~2 min), the third storage grain beside daily `metrics` and interval
-- `sessions`. One row per (source, metric, ts). Additive; safe on a populated DB.
--
-- `metric` is a series key (SERIES_METRICS in app/metrics.py), distinct
-- from the daily keys even where the quantity overlaps: the heart_rate
-- sample stream is not the resting_hr_bpm daily summary. Rows are
-- written by write_sample() (plugins/base.py), which drops a null value
-- - a source's non-wear gap - rather than storing it, so `value` is
-- never NULL.
--
-- Purely additive: one new table and its index, nothing existing is
-- read, altered or dropped. IF NOT EXISTS means a database that somehow
-- already has a samples table adopts this migration without error.

CREATE TABLE IF NOT EXISTS samples (
    source     TEXT NOT NULL,
    metric     TEXT NOT NULL,
    ts         TEXT NOT NULL,   -- ISO 8601 UTC, whole seconds (iso_utc())
    value      REAL NOT NULL,
    unit       TEXT NOT NULL,
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (source, metric, ts)
);

-- Samples are read by metric over a time range.
CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples (metric, ts);
