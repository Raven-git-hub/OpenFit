-- Small key/value store for UI preferences that belong to the install
-- rather than to one browser (the home tile layout is the first user).
--
-- Purely additive: one new table, nothing existing is read, altered or
-- dropped, so this is safe to apply to a populated production database.
-- CREATE TABLE IF NOT EXISTS means a database that somehow already has a
-- settings table adopts this migration without error.
--
-- `value` is an opaque string as far as the schema is concerned - the API
-- stores and returns whatever JSON string the client PUT, and the client
-- decides what it means. Keeping it untyped means a new preference needs
-- no migration of its own.

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
