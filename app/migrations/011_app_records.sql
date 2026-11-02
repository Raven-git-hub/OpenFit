-- App records: a private JSON store per app (C2).
--
-- An app writes metrics, sessions and samples - data OpenFit knows the
-- shape of. Anything else an app keeps (a trip report, a log entry, a
-- saved list) is a record: one JSON value under a key, in a collection
-- the app names, written by its handle() through ctx.put_record() and
-- read back by ctx.record()/ctx.records() (app/host.py). Each app sees
-- only its own: every read and write names `app`, the calling app's id,
-- and no argument names another.
--
-- `app` is the app's installed id - 'mood', 'openfitavi.frms' - not its
-- source (app:<id>), so a device that shares an app's id never meets its
-- records. `value` is the record as serialised JSON text, `size` its
-- length in UTF-8 bytes, kept so the per-app limit is one SUM (over the
-- index below). `created_at` is the first write, `updated_at` the
-- latest, both iso_utc() text.
--
-- In tracker.db, so every backup, download and restore carries it.
-- Purely additive: one new table and its index, nothing existing read,
-- altered or dropped. IF NOT EXISTS means a database that somehow
-- already has them adopts this migration without error.

CREATE TABLE IF NOT EXISTS app_records (
    app        TEXT NOT NULL,      -- the app's id: 'mood', 'openfitavi.frms'
    collection TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,      -- JSON
    size       INTEGER NOT NULL,   -- len(value) in bytes, UTF-8
    created_at TEXT NOT NULL,      -- iso_utc()
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app, collection, key)
);

-- An app's total - the limit put_record() holds every write to, and the
-- bytes /api/app-sources lists - is SUM(size) over its records. `size`
-- comes after `value` in the row, and a value of any size spills into
-- overflow pages, so reading it from the table walks each record's
-- value; this index holds it beside the app, so the sum never touches a
-- value at all.
CREATE INDEX IF NOT EXISTS idx_app_records_size ON app_records (app, size);
