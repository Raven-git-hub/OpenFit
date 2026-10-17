"""
An uninstalled app's data: listing it, and removing it.

Uninstalling an app keeps what it wrote under app:<id> - hand-entered
data can't be re-synced - so removing that is a separate, explicit step:
DELETE /api/app-sources/<id>, for an app that's no longer installed. It
deletes every metric, session and sample the source wrote, re-derives
the days its readings covered and clears every source role naming it
(purge_source(), the connector wipe's machinery), in one transaction.
GET /api/app-sources lists every app source with data, each marked
orphaned or not, so the page knows which can go.

It never reaches a live app's data, a device's, a webhook source's or
manual's: the id is always read as app:<id>.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from derived import SOURCE_ROLES_KEY, clear_source_roles, load_source_roles, recompute_derived
from metrics import METRICS, SERIES_METRICS

REAL_APPS = Path(__file__).parent / "fixtures" / "apps"

TODAY = date.today().isoformat()


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def install(apps_dir, app_id):
    """One of the real apps, as deployed."""
    shutil.copytree(REAL_APPS / app_id, apps_dir / app_id, ignore=shutil.ignore_patterns("__pycache__"))


def seed(conn, source, metrics=(), samples=(), sessions=()):
    """One source's rows. metrics: (date, metric, value); samples:
    (metric, ts, value); sessions: (kind, start)."""
    for day, metric, value in metrics:
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-10-03 06:00:00')",
            (day, source, metric, value, METRICS[metric]),
        )
    for metric, ts, value in samples:
        conn.execute(
            "INSERT INTO samples (source, metric, ts, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-10-03 06:00:00')",
            (source, metric, ts, value, SERIES_METRICS[metric]),
        )
    for kind, start in sessions:
        conn.execute(
            "INSERT INTO sessions (id, source, kind, \"start\", \"end\", summary_json, synced_at) "
            "VALUES (?, ?, ?, ?, NULL, '{}', '2026-10-03 06:00:00')",
            (f"{source}:{kind}:{start}", source, kind, start),
        )
    conn.commit()


def set_roles(conn, roles):
    """The source_roles row, as given - session kinds included, which only
    /api/settings can write."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SOURCE_ROLES_KEY, json.dumps(roles)),
    )
    conn.commit()


def counts(conn):
    """Rows per (table, source), across the three data tables."""
    out = {}
    for table in ("metrics", "sessions", "samples"):
        for source, n in conn.execute(f"SELECT source, COUNT(*) FROM {table} GROUP BY source"):
            out[(table, source)] = n
    return out


def picked(conn, day, metric):
    """(value, source) derived for one (date, metric), or None."""
    row = conn.execute(
        "SELECT value, source FROM derived_metrics WHERE date = ? AND metric = ?",
        (day, metric),
    ).fetchone()
    return tuple(row) if row else None


def derived(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT date, metric, value, source FROM derived_metrics ORDER BY date, metric"
    )]


def stored_roles(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SOURCE_ROLES_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


ROLES = {
    "weight_kg": "app:old",       # the orphan's: it wins weight_kg
    "sleep": "app:old",           # a session kind's, also the orphan's
    "steps": "google_health",     # nothing to do with it
    "distance_m": "app:steps",    # the live app's
}


@pytest.fixture
def orphan(client, conn, apps_dir):
    """app:old, whose app is gone, with data of every grain; beside it
    the installed steps app's own app:steps, Garmin, manual history and a
    pushed apple_health - none of them old's.

    2026-10-01: weight from app:old only. 2026-10-02: weight from app:old
    and Garmin, app:old winning it as weight's primary; steps from both,
    Garmin winning. 2026-09-15: a derived row with no readings, outside
    app:old's span - as a re-derive of all history would never leave it.
    """
    install(apps_dir, "steps")
    seed(
        conn, "app:old",
        metrics=[
            ("2026-10-01", "weight_kg", 80.0),
            ("2026-10-02", "weight_kg", 81.0),
            ("2026-10-02", "steps", 500),
        ],
        samples=[("heart_rate", "2026-10-01T12:00:00Z", 70)],
        sessions=[("sleep", "2026-09-30T22:00:00Z")],
    )
    seed(conn, "app:steps", metrics=[("2026-10-01", "distance_m", 3000)])
    seed(
        conn, "garmin",
        metrics=[("2026-10-02", "weight_kg", 82.0), ("2026-10-02", "steps", 9000)],
        samples=[("heart_rate", "2026-10-01T12:00:00Z", 72)],
        sessions=[("sleep", "2026-09-30T22:05:00Z")],
    )
    seed(conn, "manual", metrics=[("2026-09-01", "weight_kg", 79.0)])
    seed(conn, "apple_health", metrics=[("2026-10-03", "weight_kg", 83.0)])
    set_roles(conn, ROLES)
    recompute_derived(conn)
    conn.execute(
        "INSERT INTO derived_metrics (date, metric, value, unit, source, synced_at) "
        "VALUES ('2026-09-15', 'steps', 1234, 'count', 'google_health', 'x')"
    )
    conn.commit()

    assert picked(conn, "2026-10-01", "weight_kg") == (80.0, "app:old")
    assert picked(conn, "2026-10-02", "weight_kg") == (81.0, "app:old")
    assert picked(conn, "2026-10-02", "steps") == (9000, "garmin")


# ---------- listing: GET /api/app-sources ----------


def test_nothing_from_an_app_lists_nothing(client, conn, apps_dir):
    seed(conn, "garmin", metrics=[("2026-10-02", "steps", 9000)])

    assert client.get("/api/app-sources").get_json() == []


def test_every_app_source_is_listed_orphaned_or_not(client, orphan):
    assert client.get("/api/app-sources").get_json() == [
        {
            "id": "old", "source": "app:old", "orphaned": True,
            # Vocabulary order: steps (Activity) before weight_kg.
            "metrics": ["steps", "weight_kg"],
            "readings": 3, "sessions": 1, "samples": 1,
        },
        {
            "id": "steps", "source": "app:steps", "orphaned": False,
            "metrics": ["distance_m"],
            "readings": 1, "sessions": 0, "samples": 0,
        },
    ]


def test_only_the_app_prefix_counts(client, conn, apps_dir):
    """Not a source that merely starts "app", nor one cased differently."""
    seed(conn, "apple_health", metrics=[("2026-10-02", "steps", 1)])
    seed(conn, "app", metrics=[("2026-10-02", "steps", 2)])
    seed(conn, "APP:shout", metrics=[("2026-10-02", "steps", 3)])
    seed(conn, "app:z", samples=[("spo2", "2026-10-02T03:00:00Z", 96)])

    listed = client.get("/api/app-sources").get_json()
    assert [a["source"] for a in listed] == ["app:z"]
    assert listed[0] == {
        "id": "z", "source": "app:z", "orphaned": True, "metrics": [],
        "readings": 0, "sessions": 0, "samples": 1,
    }


def test_an_app_reinstalled_is_live_again(client, conn, apps_dir):
    seed(conn, "app:weight", metrics=[("2026-10-02", "weight_kg", 80.0)])
    assert client.get("/api/app-sources").get_json()[0]["orphaned"] is True

    install(apps_dir, "weight")

    assert client.get("/api/app-sources").get_json()[0]["orphaned"] is False


# ---------- removing: DELETE /api/app-sources/<id> ----------


def test_removing_an_orphan_deletes_everything_it_wrote(client, conn, orphan):
    before = counts(conn)

    resp = client.delete("/api/app-sources/old")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "removed": {"metrics": 3, "sessions": 1, "samples": 1}}
    after = counts(conn)
    for table in ("metrics", "sessions", "samples"):
        assert (table, "app:old") not in after, table
    # Every other source's rows are just as they were.
    assert after == {k: n for k, n in before.items() if k[1] != "app:old"}


def test_removing_an_orphan_re_derives_what_it_fed(client, conn, orphan):
    client.delete("/api/app-sources/old")

    # A day only it filled has nothing left to derive from...
    assert picked(conn, "2026-10-01", "weight_kg") is None
    # ...a day Garmin also filled falls to Garmin...
    assert picked(conn, "2026-10-02", "weight_kg") == (82.0, "garmin")
    # ...and a pick it never won stays as it was.
    assert picked(conn, "2026-10-02", "steps") == (9000, "garmin")
    assert conn.execute(
        "SELECT COUNT(*) FROM derived_metrics WHERE source = 'app:old'"
    ).fetchone()[0] == 0


def test_removing_an_orphan_leaves_days_outside_its_span_alone(client, conn, orphan):
    """Only the span its readings covered is re-derived - never all history."""
    client.delete("/api/app-sources/old")

    assert picked(conn, "2026-09-15", "steps") == (1234, "google_health")
    assert picked(conn, "2026-09-01", "weight_kg") == (79.0, "manual")
    assert picked(conn, "2026-10-03", "weight_kg") == (83.0, "apple_health")


def test_removing_an_orphan_clears_the_roles_naming_it(client, conn, orphan):
    client.delete("/api/app-sources/old")

    # Its own go - a session kind's too; everyone else's stay.
    assert stored_roles(conn) == {"steps": "google_health", "distance_m": "app:steps"}
    roles = client.get("/api/source-roles").get_json()
    assert roles["weight_kg"] == {"primary": "garmin", "configured": False, "sources": [
        "garmin", "apple_health", "manual",
    ]}


def test_the_metric_stays_when_its_only_source_goes(client, conn, apps_dir):
    """Removing app:weight's data leaves weight_kg in the vocabulary, empty."""
    seed(conn, "app:weight", metrics=[("2026-10-02", "weight_kg", 80.0)])
    recompute_derived(conn)

    assert client.delete("/api/app-sources/weight").status_code == 200

    entry = next(
        m for m in client.get("/api/catalog?include_empty=1").get_json()["metrics"]
        if m["metric"] == "weight_kg"
    )
    assert entry["count"] == 0 and entry["sources"] == []
    assert client.get("/api/metric/weight_kg").get_json()["points"] == []
    assert client.get("/api/app-sources").get_json() == []


def test_an_installed_apps_data_is_never_removed(client, conn, orphan):
    before, roles, picks = counts(conn), stored_roles(conn), derived(conn)

    resp = client.delete("/api/app-sources/steps")

    assert resp.status_code == 400
    assert resp.get_json() == {
        "ok": False,
        "error": "'steps' is present — fix or uninstall it first; an installed app's data is never removed",
    }
    assert counts(conn) == before
    assert stored_roles(conn) == roles
    assert derived(conn) == picks


# Ways an app's folder can be there and still not load - so the scanner
# skips it, but it's broken, not gone. Each is what <apps dir>/old holds.
def _bad_yaml(folder):
    folder.mkdir()
    (folder / "app.yaml").write_text("id: old\nsizes: [{w: 1, h: 1}\n")


def _no_size(folder):
    folder.mkdir()
    (folder / "app.yaml").write_text("id: old\nname: Old\n")


def _import_error(folder):
    folder.mkdir()
    (folder / "app.yaml").write_text("id: old\nsizes: [{w: 1, h: 1}]\n")
    (folder / "app.py").write_text("import not_a_module_anywhere\n")


def _no_manifest(folder):
    folder.mkdir()
    (folder / "app.py").write_text("def render(ctx, cfg):\n    return ctx.empty()\n")


def _broken_link(folder):
    folder.symlink_to(folder.parent / "nowhere")


@pytest.mark.parametrize("broken", [_bad_yaml, _no_size, _import_error, _no_manifest, _broken_link])
def test_an_app_present_but_not_loading_isnt_orphaned(client, conn, orphan, apps_dir, broken):
    """Its folder is there, so it's broken, not gone: listed live, and its
    data refused - nothing removed."""
    broken(apps_dir / "old")
    assert "old" not in [a["id"] for a in client.get("/api/apps").get_json()]
    before, roles, picks = counts(conn), stored_roles(conn), derived(conn)

    listed = {a["id"]: a["orphaned"] for a in client.get("/api/app-sources").get_json()}
    resp = client.delete("/api/app-sources/old")

    assert listed == {"old": False, "steps": False}
    assert resp.status_code == 400
    assert resp.get_json() == {
        "ok": False,
        "error": "'old' is present — fix or uninstall it first; an installed app's data is never removed",
    }
    assert counts(conn) == before
    assert stored_roles(conn) == roles
    assert derived(conn) == picks


def test_an_app_loading_from_a_folder_named_apart_from_its_id_isnt_orphaned(client, conn, orphan, apps_dir):
    """A hand-placed app's folder needn't be named after its id: no
    <apps dir>/old, but the app in scale-v2 is old - live."""
    shutil.copytree(REAL_APPS / "steps", apps_dir / "scale-v2", ignore=shutil.ignore_patterns("__pycache__"))
    manifest = apps_dir / "scale-v2" / "app.yaml"
    manifest.write_text(manifest.read_text().replace("id: steps", "id: old"))
    assert "old" in [a["id"] for a in client.get("/api/apps").get_json()]
    before = counts(conn)

    listed = {a["id"]: a["orphaned"] for a in client.get("/api/app-sources").get_json()}
    resp = client.delete("/api/app-sources/old")

    assert listed["old"] is False
    assert resp.status_code == 400
    assert counts(conn) == before


@pytest.mark.parametrize("app_id", [
    # Not the scanner's id rule (APP_ID), so no folder to look for -
    # even with rows under it.
    "_x", "-x", "x.y",
    # The prefix isn't stripped: this would be app:app:old.
    "app:old",
])
def test_an_id_no_app_could_have_is_never_orphaned(client, conn, orphan, app_id):
    seed(conn, f"app:{app_id}", metrics=[("2026-10-04", "steps", 7)])
    before, roles, picks = counts(conn), stored_roles(conn), derived(conn)

    listed = {a["source"]: a["orphaned"] for a in client.get("/api/app-sources").get_json()}
    resp = client.delete(f"/api/app-sources/{app_id}")

    assert listed[f"app:{app_id}"] is False
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": f"'{app_id}' isn't an app id: no app source to remove"}
    assert counts(conn) == before
    assert stored_roles(conn) == roles
    assert derived(conn) == picks


@pytest.mark.parametrize("app_id", [
    # Devices, the webhook's sources and manual: never app sources.
    "garmin", "google_health", "webhook", "apple_health", "manual",
    # An app with no data at all.
    "nope",
])
def test_anything_but_an_orphaned_app_source_is_a_404(client, conn, orphan, app_id):
    before, roles, picks = counts(conn), stored_roles(conn), derived(conn)

    resp = client.delete(f"/api/app-sources/{app_id}")

    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": f"no data from app:{app_id} to remove"}
    assert counts(conn) == before
    assert stored_roles(conn) == roles
    assert derived(conn) == picks


def test_a_404_leaves_a_role_naming_the_app_alone(client, conn, apps_dir):
    """Nothing to remove changes nothing - not even a role set ahead of data."""
    set_roles(conn, {"weight_kg": "app:ghost"})

    assert client.delete("/api/app-sources/ghost").status_code == 404

    assert stored_roles(conn) == {"weight_kg": "app:ghost"}


def test_a_second_removal_is_a_404(client, conn, orphan):
    assert client.delete("/api/app-sources/old").status_code == 200
    assert client.delete("/api/app-sources/old").status_code == 404


def test_a_removal_is_one_transaction(client, conn, orphan, monkeypatch):
    """A re-derive that fails takes the whole removal with it - roles included."""
    import main

    before, picks = counts(conn), derived(conn)

    def broken(*args, **kwargs):
        raise RuntimeError("re-derive failed")

    monkeypatch.setattr(main, "recompute_derived", broken)

    with pytest.raises(RuntimeError):
        client.delete("/api/app-sources/old")

    assert counts(conn) == before
    assert stored_roles(conn) == ROLES
    assert derived(conn) == picks


def test_removal_is_a_delete(client, orphan):
    assert client.post("/api/app-sources/old").status_code == 405
    assert client.get("/api/app-sources/old").status_code == 405


def test_uninstall_then_remove_the_weight_apps_data(client, conn, apps_dir):
    """End to end: log a weigh-in, uninstall the app - its data stays - then
    remove it."""
    install(apps_dir, "weight")
    resp = client.post("/api/app/weight/action", json={"action": "log", "payload": {"weight_kg": 81.2}})
    assert resp.get_json()["ok"] is True
    assert client.put("/api/source-roles/weight_kg", json={"primary": "app:weight"}).status_code == 200
    assert picked(conn, TODAY, "weight_kg") == (81.2, "app:weight")

    # Installed: listed live, and not removable.
    assert [(a["id"], a["orphaned"]) for a in client.get("/api/app-sources").get_json()] == [("weight", False)]
    assert client.delete("/api/app-sources/weight").status_code == 400

    # Uninstalled: the data stays, now orphaned.
    assert client.delete("/api/apps/weight").status_code == 200
    assert counts(conn) == {("metrics", "app:weight"): 1}
    assert client.get("/api/app-sources").get_json() == [{
        "id": "weight", "source": "app:weight", "orphaned": True,
        "metrics": ["weight_kg"], "readings": 1, "sessions": 0, "samples": 0,
    }]

    resp = client.delete("/api/app-sources/weight")

    assert resp.get_json() == {"ok": True, "removed": {"metrics": 1, "sessions": 0, "samples": 0}}
    assert counts(conn) == {}
    assert picked(conn, TODAY, "weight_kg") is None
    assert load_source_roles(conn) == {}
    assert client.get("/api/source-roles").get_json()["weight_kg"]["configured"] is False
    assert client.get("/api/app-sources").get_json() == []


# ---------- clear_source_roles() ----------


def test_clear_source_roles_drops_only_the_entries_naming_the_source(conn):
    set_roles(conn, ROLES)

    assert clear_source_roles(conn, "app:old") == ["sleep", "weight_kg"]
    conn.commit()

    assert stored_roles(conn) == {"steps": "google_health", "distance_m": "app:steps"}


def test_clear_source_roles_commits_nothing(db_path):
    """It's a step of its caller's transaction: rolled back, nothing changed."""
    writer = sqlite3.connect(db_path)
    set_roles(writer, ROLES)

    assert clear_source_roles(writer, "app:old") == ["sleep", "weight_kg"]
    writer.rollback()

    assert stored_roles(writer) == ROLES
    writer.close()


@pytest.mark.parametrize("stored", [None, "not json", "[1, 2]", '{"steps": "garmin"}'])
def test_clear_source_roles_with_none_naming_it_leaves_the_row_as_it_was(conn, stored):
    if stored is not None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (SOURCE_ROLES_KEY, stored))
        conn.commit()

    assert clear_source_roles(conn, "app:old") == []
    conn.commit()

    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SOURCE_ROLES_KEY,)).fetchone()
    assert (row[0] if row else None) == stored
