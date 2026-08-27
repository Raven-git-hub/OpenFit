# OpenFit — guide for Claude Code

Self-hosted, plugin-based personal fitness tracker. Privacy-first: your health
data lives on your own hardware and nothing leaves your network except a
plugin's own calls to its own device API.

## Stack (keep it lightweight — no frameworks without a real reason)
- Python + Flask, raw `sqlite3` (NOT SQLAlchemy/an ORM)
- Vanilla JS frontend, **no build step** — a single `app/templates/index.html`
- Docker + docker-compose; runs as one container, SQLite at `/data/tracker.db`
- Do NOT introduce Alembic, SQLAlchemy, React, a bundler, or similar.

## Layout
- `app/main.py` — Flask API + APScheduler. `serve()` runs migrations then the
  scheduler then the app; the scheduler must NOT start on import (tests import it).
- `app/migrations/` — hand-rolled migration runner. Numbered `NNN_name.sql` files
  applied once each, tracked in `schema_migrations`. Run at startup, or by hand:
  `python -m migrations [db]`. New schema change = a new numbered migration.
- `app/plugins/` — sync plugins. Each is `app/plugins/<name>/plugin.py` subclassing
  `SyncPlugin` (`app/plugins/base.py`), registered in `app/plugins/__init__.py`.
  New device/service integrations follow this pattern — never bypass it. A plugin
  declares a connector manifest (`fields`, `add_flow`) and reads its credentials
  via `self.get_credentials(conn)`; the UI's add-a-device form is built from that
  manifest, so adding a device needs no frontend code.
- `app/secrets.py` — Fernet encrypt/decrypt for stored credentials. Shares a name
  with the stdlib `secrets` module, which it shadows while `/app` leads `sys.path`;
  don't import stdlib `secrets` from anything `main.py` reaches.
- `tests/` — pytest against a temp DB, no network (Garmin/Google are never called).
- `docs/design/` — design references for the frontend.

## Data model
- Tables: `weights`, `workouts`, `activity`, `settings`, `accounts`,
  `schema_migrations`. `activity` is keyed `(date, source)` — one row per date per
  device. `accounts` is one row per plugin, holding that device's credentials as an
  encrypted JSON blob — never store or return them in the clear.
- Plugins upsert with `ON CONFLICT(date, source) DO UPDATE ... COALESCE(...)` and
  write their own `id` into `source`. `GET /api/activity` merges per-source rows to
  one flat row per date; `?by_source=1` returns raw rows.

## Hard constraints
- **No multi-tenancy in the core app.** No `user_id` columns, no auth on the core.
  Multi-user is a future per-container "Host" layer, not row-level tenancy.
- **Privacy-first:** no telemetry, no analytics, no external calls except a
  plugin's own device API, no cloud dependencies. Device credentials are encrypted
  at rest and never returned over the API.
- **Frontend:** responsive via viewport breakpoints (never device detection);
  theme-aware light+dark; all colours/fonts as CSS variables in the
  `CUSTOMISE OPENFIT HERE` token block. Starts blank — no hardcoded user goals.

## Workflow
- Always work on a branch and open a PR — **never push to `main`**.
- Migrations must be additive and safe to run against a populated production DB.
- Any new API route gets a pytest test matching the existing style.
- Run the suite with `pytest` before opening a PR.

## Roadmap
See `ROADMAP.md`. Phase 1 (migrations, per-source activity, tests) is done; current
work is the frontend rebuild and Phase 2 insights.
