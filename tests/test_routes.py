"""API route tests - the shape the frontend depends on."""

import os
import re
import sqlite3
from datetime import date

from derived import recompute_derived
from migrations import run_migrations


def test_weights_round_trip(client):
    assert client.get("/api/weights").get_json() == []

    assert client.post("/api/weights", json={"date": "2026-01-02", "weight": 81.5}).status_code == 200
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})

    rows = client.get("/api/weights").get_json()
    assert rows == [
        {"date": "2026-01-01", "weight": 82.0},
        {"date": "2026-01-02", "weight": 81.5},
    ]


def test_weights_post_upserts_same_date(client):
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 81.0})

    rows = client.get("/api/weights").get_json()
    assert rows == [{"date": "2026-01-01", "weight": 81.0}]


def test_weights_post_requires_weight(client):
    resp = client.post("/api/weights", json={"date": "2026-01-01"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_weights_delete_clears_all(client):
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})
    assert client.delete("/api/weights").status_code == 200
    assert client.get("/api/weights").get_json() == []


def test_weights_are_manual_weight_kg_readings(client, conn):
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82})

    assert [tuple(r) for r in conn.execute(
        "SELECT date, source, metric, value, unit FROM metrics"
    )] == [("2026-01-01", "manual", "weight_kg", 82.0, "kg")]
    # Read back as stored - a float, as the old REAL column gave - not
    # cast to a whole number the way steps are.
    rows = client.get("/api/weights").get_json()
    assert rows == [{"date": "2026-01-01", "weight": 82.0}]
    assert isinstance(rows[0]["weight"], float)


def test_weights_post_defaults_to_today(client):
    client.post("/api/weights", json={"weight": 80.5})

    assert client.get("/api/weights").get_json() == [
        {"date": date.today().isoformat(), "weight": 80.5}
    ]


def test_weights_are_manual_weight_only(client, conn):
    # Other sources' weight and other manual metrics share the table:
    # /api/weights neither lists them nor clears them.
    others = [
        ("2026-01-01", "manual", "steps", 9000, "count"),
        ("2026-01-01", "scale", "weight_kg", 83.0, "kg"),
        ("2026-01-02", "garmin", "steps", 8000, "count"),
    ]
    conn.executemany(
        "INSERT INTO metrics (date, source, metric, value, unit) VALUES (?, ?, ?, ?, ?)",
        others,
    )
    conn.commit()
    client.post("/api/weights", json={"date": "2026-01-02", "weight": 82.0})

    assert client.get("/api/weights").get_json() == [{"date": "2026-01-02", "weight": 82.0}]

    client.delete("/api/weights")

    assert client.get("/api/weights").get_json() == []
    assert [tuple(r) for r in conn.execute(
        "SELECT date, source, metric, value, unit FROM metrics ORDER BY date, source"
    )] == others


def test_weights_migrated_by_006_read_back_unchanged(db_at_version, monkeypatch):
    """Weigh-ins stored before 006 come out of the API byte for byte as before.

    Seeded at version 5 - the shape a production database is in - and
    the old route's answer taken straight from the weights table, then
    migrated and read back through the new one.
    """
    import main

    path = db_at_version(5)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executemany(
        "INSERT INTO weights (date, weight) VALUES (?, ?)",
        [("2026-01-02", 81.5), ("2026-01-01", 82), ("2026-01-03", 80.25)],
    )
    conn.commit()
    with main.app.app_context():
        before = main.jsonify([
            dict(r) for r in conn.execute("SELECT date, weight FROM weights ORDER BY date")
        ]).get_data()
    assert 6 in run_migrations(conn)
    conn.close()

    monkeypatch.setitem(main.app.config, "DB_PATH", path)
    after = main.app.test_client().get("/api/weights")

    assert after.get_data() == before
    assert after.get_json() == [
        {"date": "2026-01-01", "weight": 82.0},
        {"date": "2026-01-02", "weight": 81.5},
        {"date": "2026-01-03", "weight": 80.25},
    ]


def test_workouts_round_trip(client):
    assert client.get("/api/workouts").get_json() == {}

    assert client.post("/api/workouts", json={"week": 1, "idx": 0, "done": True}).status_code == 200
    client.post("/api/workouts", json={"week": 1, "idx": 1, "done": False})

    assert client.get("/api/workouts").get_json() == {"1": {"0": True, "1": False}}


def test_workouts_post_upserts(client):
    client.post("/api/workouts", json={"week": 2, "idx": 3, "done": True})
    client.post("/api/workouts", json={"week": 2, "idx": 3, "done": False})

    assert client.get("/api/workouts").get_json() == {"2": {"3": False}}


def test_workouts_post_requires_week_and_idx(client):
    resp = client.post("/api/workouts", json={"done": True})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_activity_empty(client):
    assert client.get("/api/activity").get_json() == []


def test_activity_returns_flat_rows(client, conn):
    conn.executemany(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-01-01', 'garmin', ?, ?, ?, 'now')",
        [
            ("steps", 9000, "count"),
            ("resting_hr_bpm", 52, "bpm"),
        ],
    )
    conn.commit()
    recompute_derived(conn)

    rows = client.get("/api/activity").get_json()
    assert rows == [
        {
            "date": "2026-01-01",
            "steps": 9000,
            "resting_hr": 52,
            "source": "garmin",
        }
    ]


def test_activity_days_limit_counts_dates_not_rows(client, conn):
    # Two sources and two metrics per day: limiting rows instead of
    # dates would return one day here rather than two.
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        for src in ("garmin", "google_health"):
            for metric, unit in (("steps", "count"), ("resting_hr_bpm", "bpm")):
                conn.execute(
                    "INSERT INTO metrics (date, source, metric, value, unit) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (d, src, metric, unit),
                )
    conn.commit()
    recompute_derived(conn)

    rows = client.get("/api/activity?days=2").get_json()
    assert [r["date"] for r in rows] == ["2026-01-03", "2026-01-02"]


def test_plugins_list(client):
    plugins = client.get("/api/plugins").get_json()
    ids = {p["id"] for p in plugins}
    assert {"garmin", "google_health"} <= ids
    for plugin in plugins:
        assert set(plugin) >= {"id", "name", "configured", "missing_env"}


def test_sync_unknown_plugin_404(client):
    resp = client.post("/api/sync/does_not_exist")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["ok"] is False
    assert "does_not_exist" in body["error"]


def test_index_serves_the_data_console(client):
    resp = client.get("/")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    resp.close()
    # The console is wired to these reads; renaming one breaks the page.
    for route in ("/api/catalog?include_empty=1", "/api/coverage", "/api/source-roles", "/api/connectors"):
        assert route in page


def test_index_has_the_home_and_data_tabs_and_no_activity_read(client):
    resp = client.get("/")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    resp.close()
    # The tab strip: Home (the app tiles) and Data (the console, open on load).
    assert 'data-tab="home"' in page and 'data-panel="home"' in page
    assert 'data-tab="data"' in page and 'data-panel="data"' in page
    # The last-sync stamp went, and /api/activity with it.
    assert "/api/activity" not in page


def test_index_draws_home_from_the_app_host(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    # Home lists the installed apps and renders each one's spec.
    assert "'/api/apps'" in page
    assert "'/api/app/'+enc(id)+'/render'" in page
    # The display vocabulary the renderer draws (host.py's builders).
    for spec in ("number:", "line:", "panel:", "empty:", "error:"):
        assert spec in page
    # No Configure, grid lines or drag in this slice: tiles only flow.
    assert "const layoutTiles = autoLayout;" in page


def test_index_lays_the_data_tab_out_in_two_columns(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    # Connected sources over Your data on the left, Applications on the right.
    cols = re.search(
        r'<div class="console">\s*<div class="console-col">(.*?)</div>\s*'
        r'<div class="console-col">(.*?)</div>\s*</div>',
        page,
        re.S,
    )
    assert cols, "the console's two columns"
    left, right = cols.groups()
    assert left.index('id="sourcesCard"') < left.index('id="dataCard"')
    assert right.index('id="appsCard"') < right.index('id="appListCard"')
    assert "sourcesCard" not in right

    # Two even columns that grow with the window, under a cap wider than
    # the old fixed 940px.
    console = re.search(r"^\.console\{([^}]*)\}", page, re.M).group(1)
    assert "display:grid" in console
    assert "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in console
    assert int(re.search(r"max-width:(\d+)px", console).group(1)) > 940

    # The collapse floor: a narrow window drops to one column, as .dpanel
    # does on a phone - above the phone breakpoint, so a narrow desktop
    # window stacks too.
    floor = re.search(
        r"@media \(max-width:(\d+)px\)\{\s*\.console\{grid-template-columns:minmax\(0,1fr\);", page
    )
    assert floor, "the one-column collapse rule"
    assert int(floor.group(1)) > 860


def _js_function(page, name):
    """The body of a top-level function in the page's script."""
    m = re.search(rf"\n  function {name}\(\w*\)\{{\n(.*?)\n  \}}\n", page, re.S)
    assert m, name
    return m.group(1)


def test_index_splits_applications_into_controls_and_a_list(client, monkeypatch):
    import main

    # What the list shows: the installed apps, as /api/apps returns them -
    # the two real apps.
    fixtures = os.path.join(os.path.dirname(__file__), "fixtures", "apps")
    monkeypatch.setitem(main.app.config, "APPS_DIR", fixtures)
    apps = client.get("/api/apps").get_json()
    assert [(a["id"], a["name"]) for a in apps] == [("steps", "Steps"), ("steps-metres", "Distance (from steps)")]

    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    # Two blocks, stacked in the right column: the controls card, then the
    # list panel straight under it.
    assert re.search(
        r'<section class="card" id="appsCard" aria-label="Applications"></section>\s*'
        r'<section class="card applist" id="appListCard" aria-label="Installed applications"></section>',
        page,
    )
    # Read from the same route as Home; the controls painted once at boot,
    # the list at boot and again once loaded.
    assert "'/api/apps'" in page
    boot = page[page.index("BOOT - paint the shell") :]
    assert "paintAppControls();" in boot and "paintApps();" in boot and "loadApps();" in boot

    # The controls card: the title and voice button (as Connected sources',
    # on its own clip - a placeholder until it's recorded: a 404 just resets
    # the button), then the buttons. No app rows, and none of the list's
    # states.
    controls = _js_function(page, "paintAppControls")
    assert "$('#appsCard')" in controls
    assert "cardTitle('Applications', 'apps')" in controls
    assert "appActionsRow()" in controls
    for list_part in ("appsList", "appRow", "No applications installed", "Retry", "#appListCard"):
        assert list_part not in controls, list_part
    assert "cardTitle('Connected sources', 'sources')" in page
    assert "apps:'/audio/applications.mp3'" in page

    # The list panel: one row per app in a container, as Your data's rows
    # are, its name escaped - it's data - plus the empty and error states.
    # No title, and none of the controls.
    listing = _js_function(page, "paintApps")
    assert "$('#appListCard')" in listing
    assert "el('div','alist')" in listing
    assert "appsList.apps.forEach(a => list.appendChild(appRow(a)));" in listing
    assert "'No applications installed'" in listing
    assert "Couldn't load your applications." in listing and ">Retry</button>" in listing
    for control in ("cardTitle(", "appActionsRow", "comingSoon", "#appsCard"):
        assert control not in listing, control
    row = _js_function(page, "appRow")
    assert "el('div','aitem'" in row and '<div class="arow">' in row
    assert "esc(a.name || a.id)" in row

    # The controls' buttons: Add Application in Add source's style, then
    # Browse / Build / Create, each opening the coming-soon placeholder.
    assert "const btn = el('button','btn sm','＋ Add source');" in page
    actions = _js_function(page, "appActionsRow")
    assert "button('add-app', '＋ Add Application', 'Add an application')" in actions
    for act, label in (("browse", "Browse"), ("build", "Build"), ("create", "Create")):
        assert re.search(rf"\['{act}',\s*'{label}',", page), label
    assert "const b = el('button','btn sm', esc(label));" in actions
    assert "b.onclick = () => comingSoon(title);" in actions


def test_the_applications_card_only_reads(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    # This slice is view-only: the card's code reads /api/apps and nothing
    # else - no POST, PUT or DELETE, nothing written.
    start = page.index("APPLICATIONS - the right-hand column")
    section = page[start : page.index("HOME - the installed apps' tiles", start)]
    assert "api.apps()" in section
    assert re.findall(r"api\.\w+", section) == ["api.apps"]
    for write in ("send(", "fetch(", "'POST'", "'PUT'", "'DELETE'", "localStorage"):
        assert write not in section, write


def test_index_wires_the_voice_buttons_to_the_audio_route(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    for clip in ("/audio/connected-sources.mp3", "/audio/your-data.mp3"):
        assert clip in page


def test_audio_serves_a_clip_from_audio_dir(client, tmp_path, monkeypatch):
    import main

    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "your-data.mp3").write_bytes(b"ID3 not really an mp3")
    monkeypatch.setitem(main.app.config, "AUDIO_DIR", str(audio_dir))

    resp = client.get("/audio/your-data.mp3")
    assert resp.status_code == 200
    assert resp.mimetype == "audio/mpeg"
    assert resp.get_data() == b"ID3 not really an mp3"
    resp.close()


def test_the_voice_clips_ship_in_the_repo_and_are_served(client, monkeypatch):
    import main

    # The clips are committed in app/audio/ and baked into the image there
    # (/app/audio, AUDIO_DIR's default), so a deploy carries them.
    audio_dir = os.path.join(os.path.dirname(main.__file__), "audio")
    clips = ("connected-sources.mp3", "your-data.mp3")
    for clip in clips:
        assert os.path.isfile(os.path.join(audio_dir, clip)), clip

    monkeypatch.setitem(main.app.config, "AUDIO_DIR", audio_dir)
    for clip in clips:
        resp = client.get(f"/audio/{clip}")
        assert resp.status_code == 200, clip
        assert resp.mimetype == "audio/mpeg"
        resp.close()


def test_audio_404_when_the_clip_is_absent(client, tmp_path, monkeypatch):
    import main

    # No audio folder at all.
    monkeypatch.setitem(main.app.config, "AUDIO_DIR", str(tmp_path / "audio"))
    assert client.get("/audio/your-data.mp3").status_code == 404

    # The folder, without that clip; and nothing outside it is reachable.
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "your-data.mp3").write_bytes(b"ID3")
    (tmp_path / "tracker.db").write_bytes(b"not for the browser")
    assert client.get("/audio/connected-sources.mp3").status_code == 404
    assert client.get("/audio/..%2Ftracker.db").status_code == 404
    assert client.get("/audio/../tracker.db").status_code == 404
