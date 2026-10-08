# OpenFit Roadmap

> Architecture direction and reasoning:
> `docs/architecture/openfit-overview-and-design-pathway.md`. Read that first for
> what OpenFit *is*; this file is the phase plan.

## What OpenFit is (and isn't)

OpenFit is a **database manager + app host**, not a tracker. It ingests from your
devices, derives and maintains one dataset you own (the **OpenFit Derived
Data**), and exposes it to apps. It ships **no tracking apps** — weight logs,
planners, dashboards and insights are all external apps that read the data.

## Where this sits in the landscape

- **[wger](https://github.com/wger-project/wger)** — mature self-hosted tracker
  with a real UI; thin on wearable sync.
- **[Open Wearables](https://github.com/the-momentum/open-wearables)** —
  self-hosted normaliser for Garmin/Oura/Whoop/Fitbit, but developer
  infrastructure with no product on top.

OpenFit's niche: the normalised, owned dataset of Open Wearables *plus* a host
for apps (where wger has a UI) — but OpenFit ships none of the apps itself. An
open, self-hosted equivalent of Apple HealthKit / Google Health Connect.

## The one decision that shapes everything — resolved

One container per user, managed by a shared host (like Umbrel/YunoHost manage
self-hosted app fleets). The app needs zero changes — each person's container has
its own SQLite file, so isolation is physical, not row-level. No `user_id`, no
auth on core. The provisioning complexity moves to a separate Host service (last
phase).

## Phase 1 — Data layer hardening — DONE

A hand-rolled versioned migration runner, `activity` reshaped to one row per
`(date, source)`, and a pytest suite (routes + plugin contract + migrations, temp
DB, no network).

## Phase 2 — The engine (the OpenFit Derived Data)  ← current focus

- Tidy `metrics(date, source, metric, value, unit, synced_at)` + `sessions`
  model + a canonical vocabulary; migration `005`; fold the `weights` table in as
  a `weight_kg` metric. A new metric becomes data, not a migration.
- Per-metric **source roles** (one controlling source, others verify) with an
  OpenFit default the user can override; a stored **derived value** tagged with
  its source; full history retained. Simple — no trust-scoring engine.
- Ingestion: pull connectors (Garmin, then Oura, then the Google Health API) plus
  **webhook push** (`POST /api/webhook/<token>`) for automations and on-device
  sources.

`GET /api/activity` keeps its current flat shape as a compatibility shim through
this work; the interim UI isn't touched. The orphaned `workouts` table is removed.

## Phase 3 — The access layer + a barebones viewer

- The read **contract** apps use: metric discovery ("what do I have") and range
  queries ("metric X over range Y", with provenance).
- A minimal built-in **viewer** to see the data graphically — the first consumer
  of the contract and a way to confirm the engine. A verification view, not a
  tracker.

## Phase 4 — The app host

A sandboxed mechanism to run **external/imported apps** against the contract
(no-build, no network beyond the contract). Trackers, insight dashboards and
planners all live here as external apps — none are bundled. Charting stays within
the no-build constraint (hand-rolled SVG or a no-build library via a plain
`<script>`, no bundler).

## Phase 5 — OpenFit Host (per-user provisioning)

A separate project: reverse proxy with dynamic routing, a control-plane service
that spins up a container per user via the Docker API, per-container backups, a
minimal admin view. Sequenced after the platform itself is solid.

## Community-readiness (ongoing)

`CONTRIBUTING.md` + issue templates, CI (lint + the test suite on every PR),
more plugins as contributions (Oura the natural next), versioned releases once
past prototype.

## Immediate next steps (Phase 2 slices — each a branch/PR)

1. Metrics model — migration `005` + plugin contract + `/api/activity` compat.
2. Fold weight into metrics.
3. Sessions (sleep stages, workouts).
4. Source roles + derived value + history.
5. Webhook ingestion.
6. The access contract — paired design first.
