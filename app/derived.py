"""
The derived value: one trusted reading per metric per day.

metrics keeps every source's reading, so a day can hold steps from both
Garmin and Google Health. Apps shouldn't have to reconcile that
themselves: OpenFit picks one reading per (date, metric) and stores it in
derived_metrics (migration 008), tagged with the source it came from.
This module is the only place that pick is made - /api/activity reads
the stored result rather than choosing again.

When it runs: after each sync, over the window that sync wrote
(recompute_derived(conn, since=...)), after each webhook reading, over
that reading's day (since=until=its date), and once on first boot to
fill a database whose readings predate derived_metrics
(backfill_derived()).
Never as a blanket re-derive of all history on startup: the policy is
configurable, and changing it is forward-only, so values already derived
must not be rewritten under a policy they weren't picked with.

The policy: one default source precedence (DEFAULT_PRIORITY) for every
metric, which the user can override per metric by naming that metric's
primary source - its source role, kept as JSON in the settings row
SOURCE_ROLES_KEY (see load_source_roles()). A metric with no override is
picked exactly by the default.
"""

import json

from metrics import unit_for

# Order of trust when two sources report the same metric for the same day.
# Earlier wins. Sources not listed here (e.g. 'unknown' rows migrated from
# before the source column was populated, or 'manual' entry) rank last,
# ordered by name so the result is deterministic.
DEFAULT_PRIORITY = ["garmin", "google_health"]


# The settings key holding the source roles: a JSON object mapping a
# canonical metric to the source configured as its primary, e.g.
# {"steps": "google_health"}. A metric absent from it uses the default.
SOURCE_ROLES_KEY = "source_roles"


def source_rank(source, primary=None):
    """Sort key for a source: the metric's primary, then priority order, then name.

    `primary` is the source configured as the metric's primary, if any:
    it outranks every other source, and the rest keep the default order.
    """
    if primary is not None and source == primary:
        return (-1, "")
    try:
        return (DEFAULT_PRIORITY.index(source), "")
    except ValueError:
        return (len(DEFAULT_PRIORITY), source or "")


def recompute_derived(conn, since=None, until=None):
    """Re-pick the derived value of every (date, metric) from `since` to `until`.

    `since` and `until` are ISO dates ('YYYY-MM-DD'), both inclusive;
    None leaves that end open, so neither means every date. A sync
    passes only `since` (its window runs to today); a single pushed
    reading passes the same date as both, re-deriving just its day. For
    each (date, metric) in metrics within that range, the reading from
    the highest-ranked source wins - the metric's configured primary
    (load_source_roles()) if it has one, then DEFAULT_PRIORITY - and is
    upserted into derived_metrics with its value, unit and synced_at, and
    `source` naming where it came from. A source that didn't report the
    metric that day has no row in metrics (a reading is never NULL), so
    the pick falls through to the next source that did: a primary that
    missed a day hands it to the default order.

    A derived row in range whose readings have all gone - every source's
    row deleted - is deleted too, so nothing derived outlives its data.
    Dates outside the range are left exactly as they are.

    Commits, like sync(): it's a batch of its own. Returns the number of
    derived values written.
    """
    bounds = [("date >= ?", since), ("date <= ?", until)]
    in_range = " AND ".join(clause for clause, d in bounds if d is not None) or "1"
    params = tuple(d for _, d in bounds if d is not None)

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

    # Read inside that transaction too, so a source role changed mid-way
    # applies to all of this pick or none of it.
    roles = load_source_roles(conn)

    # (date, metric) -> (rank of its best source so far, that reading)
    best = {}
    for date, metric, value, unit, source, synced_at in conn.execute(
        "SELECT date, metric, value, unit, source, synced_at FROM metrics "
        f"WHERE {in_range}",
        params,
    ):
        rank = source_rank(source, roles.get(metric))
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


def load_source_roles(conn):
    """The configured primary source per metric: {metric: source}.

    Read from the SOURCE_ROLES_KEY settings row; no row means no
    overrides. A value that isn't a JSON object, or an entry that isn't a
    non-empty string, is ignored rather than raised: the row can also be
    written through the generic /api/settings route, and a bad value
    there must not stop every sync from deriving - the metric just falls
    back to the default.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SOURCE_ROLES_KEY,)
    ).fetchone()
    try:
        roles = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return {}
    if not isinstance(roles, dict):
        return {}
    return {
        metric: source
        for metric, source in roles.items()
        if isinstance(source, str) and source
    }


def set_source_role(conn, metric, source):
    """Make `source` the configured primary for `metric`; None clears it.

    Raises ValueError for a metric outside the vocabulary. Only stores
    the config - it re-derives nothing. The new primary applies from the
    next recompute_derived() over a window, i.e. the next sync: values
    already derived keep the source they were picked with.

    Commits.
    """
    unit_for(metric)

    # The insert goes first: it opens the write transaction, so two
    # changes at once can't both read the old map and one lose the
    # other's entry.
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, '{}')",
        (SOURCE_ROLES_KEY,),
    )
    roles = load_source_roles(conn)
    if source:
        roles[metric] = source
    else:
        roles.pop(metric, None)
    conn.execute(
        "UPDATE settings SET value = ? WHERE key = ?",
        (json.dumps(roles, sort_keys=True), SOURCE_ROLES_KEY),
    )
    conn.commit()


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
