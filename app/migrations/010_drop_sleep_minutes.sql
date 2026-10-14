-- Schema v2: retire the daily sleep_minutes metric.
--
-- Sleep was dual-track since 007: a daily sleep_minutes reading in
-- metrics, and the night as a `sleep` session beside it. Schema v2
-- (docs/architecture/metric-schema-v2.md) keeps sleep as sessions only -
-- a night's total is its session's asleep_minutes - and sleep_minutes
-- leaves the vocabulary (app/metrics.py), so no plugin files it any more.
-- This deletes the readings already filed, and the derived values picked
-- from them, so nothing stored names a key outside the vocabulary.
--
-- Touches sleep_minutes rows only, in those two tables: every other
-- metric, and the sleep sessions, are left exactly as they are. Nothing
-- is created, altered or dropped. The runner wraps this whole file in one
-- transaction, so it either lands completely or not at all.
--
-- A deliberate delete, not an additive change: a night recorded only as
-- a daily total - from before 007, when there were no sessions - goes
-- with it. A sync with a longer window (POST /api/sync/<plugin>?days=N)
-- re-fetches those nights as sessions from the device's own history.

DELETE FROM metrics WHERE metric = 'sleep_minutes';

DELETE FROM derived_metrics WHERE metric = 'sleep_minutes';
