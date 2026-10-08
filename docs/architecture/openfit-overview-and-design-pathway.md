# OpenFit — overview and design pathway

**Status:** canonical architecture direction (engine + app host; OpenFit ships no
tracking apps). Read this first; ROADMAP.md has the phases.

## What OpenFit is

OpenFit is self-hosted middleware between your health/fitness devices and the apps
that make that data useful. You plug devices in (Garmin, Oura, a Google/Fitbit
account, eventually Apple Health); OpenFit syncs them, normalises everything into
one derived dataset you own — the **OpenFit Derived Data** — and exposes it. The
closest reference point is an open, self-hosted Apple HealthKit / Google Health
Connect.

OpenFit is deliberately **not a tracker, and ships no tracking apps.** It is two
things: a **database manager** (ingest, derive one clean dataset, maintain it and
its history) and a **host for apps** that read that dataset. Every tracking app —
weight log, planner, dashboards — lives *outside* the platform and reads the data
through the access layer. A minimal built-in viewer exists only to *see* the data,
not as a bundled tracker.

The reason it exists — a hard constraint — is ownership and privacy: your health
history shouldn't live in someone else's cloud. No telemetry, no external calls
except each connector reaching its own device's API.

## How it works

Spine: **ingest → derive & maintain the OpenFit Derived Data → expose to apps.**

### Ingestion — two kinds of connector
- **Pull connectors** poll a cloud API on a schedule: Garmin (unofficial
  python-garminconnect), Oura (Cloud API v2), the Google Health API (now the cloud
  path for Fitbit + Google Fit; legacy Fitbit API retired ~Sept 2026).
- **Push connectors** POST to a webhook: on-device sources (Apple HealthKit,
  Health-Connect-only apps) via a small phone-side forwarder, plus automations and
  manual entry. The webhook contract (per-connector token, canonical payload,
  idempotent writes) is the key interface — any push source feeds one door.

### The OpenFit Derived Data
Record shapes: daily, instant, session/interval (sleep, workout), series
(intraday — deferred). Tidy storage: `metrics(date, source, metric, value, unit,
synced_at)` so a new metric is data not a migration; `sessions(id, source, kind,
start, end, summary_json, synced_at)`; a shared canonical vocabulary
(`resting_hr_bpm`, `weight_kg`, ...). Weight is just a metric.

### One source, sometimes more
Most users have one source per metric. When more, each metric has a source role —
one controlling source, others verify — with an OpenFit default the user can
override. A single derived value per `(date, metric)` is stored, tagged with its
source; changing the primary is a forward note, not a rewrite; raw per-source rows
and full history are kept. No trust-scoring engine.

### Exposing the data
Apps read through a small stable contract: discovery ("what metrics do I have")
and range queries ("metric X over range Y", with provenance). Apps are external
(imported or user-built) and run in the app host; the platform ships none. A
minimal built-in viewer is the first consumer of the contract, not a product.

## Design pathway
1. Data-layer foundation (Phase 1). DONE.
2. The engine: `metrics` + `sessions` + canonical vocabulary, fold weight in.
3. Source roles + stored derived value + history.
4. Pull connectors (Garmin, Oura, Google Health API).
5. Webhook / push ingestion — the key interface.
6. The access contract — discovery + range queries + provenance.
7. A barebones data viewer — first consumer of the contract.
8. The app host + external apps (trackers, insights — all external, none bundled).
9. Native mobile + Apple HealthKit (later).
10. OpenFit Host — per-container provisioning (later).

## Open questions parked for later
- Per-connector backfill depth (source-bounded).
- Whether the derived value ever needs a reproducible/append-only history (not now).
- The intraday series grain and its retention (deferred).
