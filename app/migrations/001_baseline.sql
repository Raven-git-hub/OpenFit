-- Baseline: the schema OpenFit shipped with before migrations existed.
--
-- Written with CREATE TABLE IF NOT EXISTS so an already-populated
-- database (one created by the old init_db() in main.py) adopts this
-- migration cleanly: nothing is created, no data is touched, and the DB
-- is simply recorded as being at version 1.

CREATE TABLE IF NOT EXISTS weights (
    date TEXT PRIMARY KEY,
    weight REAL
);

CREATE TABLE IF NOT EXISTS workouts (
    week INTEGER,
    idx INTEGER,
    done INTEGER,
    PRIMARY KEY (week, idx)
);

CREATE TABLE IF NOT EXISTS activity (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    resting_hr INTEGER,
    sleep_hours REAL,
    source TEXT,
    synced_at TEXT
);
