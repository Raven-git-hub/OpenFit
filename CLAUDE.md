# OpenFit — guide for Claude Code

Self-hosted **health-data engine + app host** — NOT a tracker. OpenFit ingests
from your devices, derives and maintains one dataset you own (the **OpenFit
Derived Data**), and exposes it to apps. It ships **no tracking apps**; every
tracker/dashboard/planner is an external app that reads the data. Privacy-first:
data lives on your own hardware, nothing leaves your network except a plugin's
own calls to its device API. Full direction:
`docs/architecture/openfit-overview-and-design-pathway.md`.

## Stack (keep it lightweight — no frameworks without a real reason)
- Python + Flask, raw `sqlite3` (NOT SQLAlchemy/an ORM)
- Vanilla JS, no build step
- Docker + docker-compose; one container, SQLite at `/data/tracker.db`
- Do NOT introduce Alembic, SQLAlchemy, React, a bundler, or similar.

## Layout
- `app/main.py` — Flask API + APScheduler. `serve()` runs migrations, then the
  scheduler, then the app; the scheduler must NOT start on import (tests import it).
- `app/migrations/` — hand-rolled runner. Numbered `NNN_name.sql`, applied once,
  tracked in `schema_migrations`. New schema change = a new numbered migration;
  migrations must be additive and safe against a populated production DB.
- `app/plugins/` — sync (pull) plugins. Each `app/plugins/<name>/plugin.py`
  subclasses `SyncPlugin` (`app/plugins/base.py`), registered in `__init__.py`,
  declares a connector manifest (`fields`, `add_flow`), reads creds via
  `self.get_credentials(conn)`, writes readings via `write_metric()` and
  interval records via `write_session()`. Never bypass this pattern.
- `app/metrics.py` — the canonical metric vocabulary (key → unit), plus the
  session kinds (`SLEEP`, `WORKOUT` in `SESSION_KINDS`). Import keys and kinds
  from here; never spell metric or kind strings out in plugins or the API.
- `app/crypto.py` — Fernet encrypt/decrypt for stored credentials.
- `tests/` — pytest, temp DB, no network.
- `docs/architecture/` — design direction (read first). `docs/design/` — frontend references.

## Data model
- Current: a tidy `metrics(date, source, metric, value, unit, synced_at)` keyed
  `(date, source, metric)` — one row per reading, so a new metric is data not a
  migration. Keys and units come from `app/metrics.py` (`steps`/count,
  `resting_hr_bpm`/bpm, `sleep_minutes`/min, `weight_kg`/kg). Migration `005`
  unpivoted the old wide `activity` table into it and dropped `activity`;
  migration `006` moved the old `weights` table in as `weight_kg` readings under
  source `manual` (hand entry; `synced_at` NULL for migrated rows) and dropped
  `weights`.
- Beside it, `sessions(id, source, kind, "start", "end", summary_json, synced_at)`
  (migration `007`, purely additive) for interval records: `id` is
  `source:kind:start` so a re-sync upserts, `start`/`end` are ISO 8601 UTC
  (`iso_utc()` in `app/plugins/base.py`), `summary_json` is the kind's
  breakdown as JSON. Quote `"start"`/`"end"` in SQL (`end` is a keyword).
  Written via `write_session()`, which raises on a `kind` outside
  `SESSION_KINDS` (`app/metrics.py`, where each kind's summary keys are listed).
  Two kinds, both from Garmin and Google; a summary holds only what the source
  gives:
  - `sleep` — one session per night/nap, summary in minutes: `asleep_minutes`
    plus `light_minutes`/`deep_minutes`/`rem_minutes`/`awake_minutes`. Sleep is
    dual-track: the daily `sleep_minutes` metric is written exactly as before
    and the session sits beside it.
  - `workout` — one session per activity, and no daily metric: `type` (the
    source's activity type, lowercased — not yet mapped across sources),
    `duration_minutes`, `distance_m`, `avg_hr_bpm`, `calories_kcal`.
  No read endpoint for sessions yet (the access contract owns reads).
- Also: `workouts` (orphaned — slated for removal); `settings`; `accounts`;
  `schema_migrations`.
- Target (in progress — see Direction): per-metric source roles pick a
  controlling source (user-overridable) and a derived value tagged with its
  source; full history retained.

## Hard constraints
- No multi-tenancy in core. No `user_id`, no auth on core. Multi-user is a future
  per-container Host layer.
- Privacy-first: no telemetry, no external calls except a plugin's own device API.
  Credentials encrypted at rest, never returned over the API.
- Ships no tracking apps. `app/templates/index.html` is a legacy/disposable UI
  slated to become a barebones data viewer — don't build tracker features into core.

## Workflow
- Always work on a branch and open a PR — never push to `main`.
- Any new API route gets a pytest test in the existing style.
- Run `pytest` before opening a PR.

## Direction (in progress — not all built yet)
See `docs/architecture/openfit-overview-and-design-pathway.md` and `ROADMAP.md`.
These are NOT yet in the code — don't assume they exist until a PR lands them, and
when you implement one, update the Data model section above and the README in the
same PR:
- **The engine:** the `metrics` table + canonical vocabulary landed in migration
  `005`; weight folded in as `weight_kg` in `006`; `sessions` in `007` (sleep,
  then workouts with no further migration). Still to come: per-metric source
  roles + stored derived value + history.
- **Webhook / push ingestion:** `POST /api/webhook/<token>` (token-as-auth,
  `{metric, value}` body, sanity checks, idempotent) beside pull plugins and
  manual entry.
- **The access contract:** a stable read API (metric discovery + range queries
  with provenance) that external apps use.
Keep `GET /api/activity` returning its current flat shape, and `/api/weights` its
`[{date, weight}]` shape (manual `weight_kg` readings only), as compatibility shims
during the data-model change. Remove the orphaned `workouts` table +
`/api/workouts` when convenient (unused by the UI).
