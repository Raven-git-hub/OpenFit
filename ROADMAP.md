# OpenFit Roadmap

## Where this sits in the landscape

Two existing projects are worth knowing about, because OpenFit is
deliberately positioned between them:

- **[wger](https://github.com/wger-project/wger)** — mature, self-hosted
  workout/nutrition/weight tracker with a real UI, apps, and years of
  community development. Its wearable-sync story is thin; pulling in
  Garmin/Fitbit-style data has never been a first-class feature.
- **[Open Wearables](https://github.com/the-momentum/open-wearables)** —
  early-stage, self-hosted, MIT-licensed platform that normalizes Garmin,
  Oura, Whoop, Fitbit, etc. into one API. Strong on the plugin/sync side,
  but it's developer infrastructure — no tracker UI, no training program,
  nothing a non-technical person would open and use.

OpenFit's niche: **a tracker with its own UI (like wger) built on a
plugin architecture for device data (like Open Wearables)**. That
combination doesn't appear to exist yet as an open-source project. Worth
saying explicitly in the repo's README/about section — it's a real reason
for this to exist, not just "yet another fitness tracker."

## The one decision that shapes everything else — resolved

Original framing was "single-user installs" vs "true multi-tenant." The
actual answer is better than either: **one container per user, spun up
and managed by a shared host.** This has real precedent — it's roughly
how Umbrel and YunoHost manage fleets of self-hosted apps, just not for
fitness data specifically.

Why this is the right call, not just the fun one:

- **The app itself needs zero changes.** Each person's container has its
  own SQLite file, so data isolation is physical, not row-level. No
  `user_id` columns, no ownership checks on every route, none of the bug
  surface real multi-tenancy carries. Everything already built stays
  exactly as-is.
- **The complexity moves to a new, separate piece** — a small "host"
  service whose only job is provisioning: spin up a new OpenFit container
  when someone's added, route traffic to the right one, handle backups
  per container. That piece doesn't exist yet and doesn't need to for a
  while — see the new Phase 3 below.
- This also means **Phase 1's schema work gets simpler**, not harder: no
  `user_id` retrofitting needed. Drop that bullet entirely.

## Phase 1 — Data layer hardening

Insights are only as good as the data underneath them, so this comes
first even though it's not the exciting part. No multi-user schema work
needed (see above) — this phase is purely about making the existing
single-user schema solid.

- Replace ad hoc `CREATE TABLE IF NOT EXISTS` with real migrations
  (Alembic, or a minimal hand-rolled versioned-migration runner) — matters
  more once there are multiple containers running different schema
  versions in the wild
- Fix the known "one row per date" limitation on `activity` — move to one
  row per date *per source*, so Garmin and Google Health data for the
  same day never contend
- Basic `pytest` suite covering the API routes and plugin `sync()`
  contract, so insights work in Phase 2 isn't built on ground that can
  shift under it

**Pairing split:** schema shape — pair, still needs your judgment on real
usage. Migration tooling and test scaffolding — I can draft, you review.

## Phase 2 — Insights & dashboards (the priority)

This is where "raw log" turns into "actually tells you something."
Candidate features, roughly in order of how much they'd tell you *right
now* with your own data:

- **Weekly summary view** — weight trend, total steps, sleep average,
  workout adherence, all in one glance instead of scattered across
  sections
- **Correlations worth surfacing:** sleep hours vs next-day resting HR,
  steps vs weekly weight change, workout adherence vs weight trend rate.
  Whether these are *useful* or just *technically correlated* is a
  judgment call — this is the part where your read on your own data
  matters more than mine
- **Smarter weight trend** — moving average instead of raw points (daily
  weight noise is real), pace-vs-target with a "you're X days
  ahead/behind schedule" readout
- Charting: the current hand-rolled SVG works for two lines, but
  multi-series correlation charts will want a real library (recharts or
  chart.js, both already available if this moves to a proper frontend
  build)
- Structurally: worth considering an **"insights" plugin pattern**
  mirroring the sync plugins — each insight is a small module that reads
  from `activity`/`weights` and returns a finding. Keeps the same "small
  isolated pieces" philosophy as the sync plugins, and means a future
  contributor can add a new insight without touching core code

**Pairing split:** this is the one to do together most closely — I can
build the plumbing (queries, chart rendering) but which correlations are
worth showing, and how to phrase/display them so they're useful rather
than just "line went up," is exactly the kind of judgment that shouldn't
be automated away from you.

## Phase 3 — OpenFit Host (the per-user provisioning layer)

This is the new piece implied by "one container per user, shared host."
Worth treating as a genuinely separate project (possibly its own repo)
rather than a feature bolted onto the tracker - it has a different shape
of problem entirely: infrastructure orchestration, not fitness data.

Rough shape of what it needs:

- **Reverse proxy with dynamic routing** (Traefik or Caddy are the usual
  choices) — so `alice.yourdomain.com` and `bob.yourdomain.com` route to
  different containers without hand-editing config each time someone's
  added
- **A control-plane service** that talks to the Docker API to spin up a
  new OpenFit container (with its own volume) when a user is added, and
  tear one down when they leave
- **Per-container backups** — since data is now scattered across N
  SQLite files instead of one, backup/restore needs to be a host-level
  concern, not something each user thinks about
- **A minimal admin view** — "add a person" triggers provisioning; that's
  probably the entire UI this needs at first

This is meaningfully more engineering than the tracker itself has needed
so far (Docker API, reverse proxy config, routing, TLS). Worth sequencing
*after* Phases 1–2 are solid — it's infrastructure around a product that
doesn't fully exist yet otherwise.

## Phase 4 — Community-readiness

Only worth doing once Phases 1–2 are solid, but flagging now so it
doesn't get forgotten:

- `CONTRIBUTING.md`, issue templates
- CI on GitHub Actions — lint + the Phase 1 test suite on every PR
- More plugins as community contributions (Oura's clean public API makes
  it the natural "good first plugin" for someone else to try)
- Actual versioned releases + changelog once it's past pure-prototype

## Immediate next steps

1. Add `pytest` scaffolding + tests for the existing API — cheap
   insurance before Phase 2 starts building on top of it
2. First insights target: the weekly summary view — smallest useful
   slice of Phase 2, and immediately useful for the wedding countdown
   itself
3. Phase 3 (the Host layer) can stay a rough sketch until 1–2 are done -
   no need to design it in detail yet
