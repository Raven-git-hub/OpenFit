-- Report files: documents an app makes, for the user to keep (C3).
--
-- An app's handle() makes one - a monthly fatigue report, say - as HTML
-- text and saves it with ctx.save_file(name, html) (app/host.py); render()
-- lists them with ctx.files() and offers each as a download
-- (ctx.download(name)). OpenFit never displays one: GET
-- /api/app/<id>/files/<name> always hands it over as a file, so its
-- script never runs inside OpenFit's pages. For a PDF, the user opens the
-- file and prints it from the browser.
--
-- `app` is the app's installed id - 'mood', 'openfitavi.frms' - as
-- app_records' is (011), never its source, so a device that shares an
-- app's id never meets its files. `name` is the file's name,
-- 'fatigue-2026-10.html' (lowercase, ending .html: host.py's FILE_NAME).
-- `content` is the HTML exactly as the app gave it, as UTF-8 bytes;
-- `size` its length, kept so the per-app limits are one SUM and one
-- COUNT over the index below. `created_at` the first save, `updated_at`
-- the latest, both iso_utc() text.
--
-- In tracker.db, not on disk: a file is saved inside the action's one
-- transaction (an action that fails leaves no file behind), and every
-- backup, download and restore carries it with everything else.
-- Purely additive: one new table and its index, nothing existing read,
-- altered or dropped. IF NOT EXISTS means a database that somehow
-- already has them adopts this migration without error.

CREATE TABLE IF NOT EXISTS app_files (
    app        TEXT NOT NULL,      -- the app's id: 'mood', 'openfitavi.frms'
    name       TEXT NOT NULL,      -- 'fatigue-2026-10.html'
    content    BLOB NOT NULL,      -- UTF-8 HTML, as given
    size       INTEGER NOT NULL,   -- bytes
    created_at TEXT NOT NULL,      -- iso_utc()
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app, name)
);

-- An app's total and count - the limits save_file() holds every save to,
-- and what /api/app-sources lists - are SUM(size) and COUNT(*) over its
-- files. `size` comes after `content` in the row, and a file of any size
-- spills into overflow pages, so reading it from the table would walk
-- each file's content; this index holds it beside the app, so neither
-- ever touches a file's bytes (as app_records' size index does, 011).
CREATE INDEX IF NOT EXISTS idx_app_files_size ON app_files (app, size);
