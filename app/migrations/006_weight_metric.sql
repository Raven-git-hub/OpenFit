-- Weight: its own weights table -> the weight_kg metric.
--
-- weights(date, weight) predates the metrics table: a silo with one
-- hand-entered number per day. Weight is just another metric, so each
-- weigh-in becomes one weight_kg reading in metrics and weights is
-- dropped. A scale plugin or the webhook can then file readings beside
-- the hand-entered ones under its own source, like any other metric.
--
-- The runner wraps this whole file in one transaction, so it either
-- lands completely or not at all.

-- The stored numbers are copied across as they are, as kilograms: the
-- UI has always stored kg (the first one asked for "Weight in kg", the
-- current one converts lb to kg before it saves), and the API carries a
-- bare number with no unit. No conversion is applied.
--
-- source is 'manual' because every weigh-in was typed in by hand - no
-- device ever wrote to this table. synced_at stays NULL: nothing was
-- ever synced, and the table never kept a time of entry to stand in for
-- one. NULL weights are skipped rather than carried over: a missing
-- reading is simply no row, never an empty one.
INSERT INTO metrics (date, source, metric, value, unit, synced_at)
SELECT date, 'manual', 'weight_kg', weight, 'kg', NULL
FROM weights
WHERE weight IS NOT NULL;

DROP TABLE weights;
