-- Device connector credentials, entered through the UI instead of .env.
--
-- One row per plugin: `credentials` is a Fernet-encrypted JSON blob of
-- that connector's manifest fields (see app/crypto.py), so a copy of
-- tracker.db is not a copy of your Garmin password. The schema stays
-- deliberately opaque - a connector that grows a field needs no
-- migration, because the shape lives in the plugin's manifest.
--
-- Purely additive: one new table, nothing existing is read, altered or
-- dropped, so this is safe to apply to a populated production database.
-- CREATE TABLE IF NOT EXISTS means a database that somehow already has
-- an accounts table adopts this migration without error.
--
-- No user_id column, on purpose: multi-user is a future per-container
-- Host layer, not row-level tenancy (see CLAUDE.md).

CREATE TABLE IF NOT EXISTS accounts (
    plugin_id TEXT PRIMARY KEY,
    credentials TEXT,
    created_at TEXT
);
