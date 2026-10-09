"""
The derived value: one trusted reading per metric per day.

metrics keeps every source's reading, so a day can hold steps from both
Garmin and Google Health. Apps shouldn't have to reconcile that
themselves: OpenFit picks one reading per (date, metric) and stores it in
derived_metrics (migration 008), tagged with the source it came from.
This module is the only place that pick is made - /api/activity reads
the stored result rather than choosing again.

When it runs: after each sync, over the window that sync wrote
(recompute_derived(conn, since=...)), and once on first boot to fill a
database whose readings predate derived_metrics (backfill_derived()).
Never as a blanket re-derive of all history on startup: once the policy
is configurable, changing it is forward-only, so values already derived
must not be rewritten under a policy they weren't picked with.

The policy, for now, is one fixed source precedence for every metric.
"""

# Order of trust when two sources report the same metric for the same day.
# Earlier wins. Sources not listed here (e.g. 'unknown' rows migrated from
# before the source column was populated, or 'manual' entry) rank last,
# ordered by name so the result is deterministic.
DEFAULT_PRIORITY = ["garmin", "google_health"]


def source_rank(source):
    """Sort key for a source: priority order first, then name."""
    try:
        return (DEFAULT_PRIORITY.index(source), "")
    except ValueError:
        return (len(DEFAULT_PRIORITY), source or "")


def recompute_derived(conn, since=None):
    """Re-pick the derived value of every (date, metric) from `since` on.

    `since` is an ISO date ('YYYY-MM-DD'); None means every date. For
    each (date, metric) in metrics within that range, the reading from
    the highest-ranked source wins, and is upserted into derived_metrics
    with its value, unit and synced_at, and `source` naming where it came
    from. A source that didn't report the metric that day has no row in
    metrics (a reading is never NULL), so the pick falls through to the
    next source that did.

    A derived row in range whose readings have all gone - every source's
    row deleted - is deleted too, so nothing derived outlives its data.
    Dates before `since` are left exactly as they are.

    Commits, like sync(): it's a batch of its own. Returns the number of
    derived values written.
    """
    in_range, params = ("date >= ?", (since,)) if since is not None else ("1", ())

    # The delete goes first: it opens the write transaction, so the read
    # below and the upsert after it see the same readings, and another
    # sync can't commit in between and leave a stale pick behind.
    conn.execute(
        f"DELETE FROM derived_metrics WHERE {in_range} AND NOT EXISTS ("
        "SELECT 1 FROM metrics m "
        "WHERE m.date = derived_metrics.date AND m.metric = derived_metrics.metric"
        ")",
        params,
    )

    # (date, metric) -> (rank of its best source so far, that reading)
    best = {}
    for date, metric, value, unit, source, synced_at in conn.execute(
        "SELECT date, metric, value, unit, source, synced_at FROM metrics "
        f"WHERE {in_range}",
        params,
    ):
        rank = source_rank(source)
        if (date, metric) not in best or rank < best[(date, metric)][0]:
            best[(date, metric)] = (rank, (date, metric, value, unit, source, synced_at))

    conn.executemany(
        "INSERT INTO derived_metrics (date, metric, value, unit, source, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date, metric) DO UPDATE SET "
        "value=excluded.value, unit=excluded.unit, "
        "source=excluded.source, synced_at=excluded.synced_at",
        [reading for _, reading in best.values()],
    )
    conn.commit()
    return len(best)


def backfill_derived(conn):
    """Derive every reading, once, if nothing has been derived yet.

    For a database upgraded past 008: its readings are all there but
    derived_metrics starts out empty, and syncs only refresh their own
    recent window. Returns whether it ran.

    Only ever into an empty table. Once anything is derived, history is
    left alone - this is a first-boot fill, not a re-derive on every
    start.
    """
    if conn.execute("SELECT 1 FROM derived_metrics LIMIT 1").fetchone():
        return False
    if not conn.execute("SELECT 1 FROM metrics LIMIT 1").fetchone():
        return False
    recompute_derived(conn)
    return True
