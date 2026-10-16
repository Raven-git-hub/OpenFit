"""API route tests - the shape the frontend depends on."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import date

import pytest

import apps
import home_layout
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
    # The layout is tabs, each a flow - an order and a size per tile -
    # read with the apps; Home shows the default tab, squared with them.
    assert "const layoutTiles = flowLayout;" in page
    assert "autoLayout" not in page
    load = _js_function(page, "loadHome")
    assert "await Promise.all([api.apps(), api.homeLayout()]);" in load
    assert "const tab = saved.tabs.find(t => t.id===saved.default_tab) || saved.tabs[0];" in load
    assert "home.tiles = layoutTiles(items, apps, elsewhere).map(it => {" in load
    # Each tile carries its instance's id and config, for the save.
    assert "return {instance_id:it.instance_id, app_id:a.id, name:a.name, spec," in load
    assert "w:it.size.w, h:it.size.h, sizes:appSizes(a), config:it.config};" in load
    assert "home.layout = saved;" in load and "home.activeTab = tab.id;" in load


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
    return _js_source(page, name, body=True)


def _js_source(page, name, body=False):
    """A top-level function in the page's script: as written, or its body."""
    m = re.search(
        rf"\n  ((?:async )?function {name}\([\w, ]*\)\{{\n(.*?)\n  \}})\n", page, re.S
    )
    assert m, name
    return m.group(2 if body else 1)


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
    assert "el('div','ditem aitem')" in row and "el('button','drow arow'," in row
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
    # ... except Add Application, which opens the install popup.
    assert "add.onclick = openAddApp;" in actions
    # The card's one line of news, for what an install did.
    assert "note.id = 'appsMsg';" in controls


def test_the_applications_list_writes_only_uninstall_and_the_popup_installs(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    start = page.index("APPLICATIONS - the right-hand column")
    popup = page.index("ADD APPLICATION - the popup that installs one", start)
    home = page.index("HOME - the installed apps' tiles", popup)

    # The controls and the list read /api/apps, an app's files and its
    # render; their one write is Uninstall. All of it through the API
    # block - the only place that knows a URL - so no raw request here.
    section = page[start:popup]
    assert "api.apps()" in section
    assert sorted(set(re.findall(r"api\.\w+", section))) == [
        "api.appFile", "api.apps", "api.renderApp", "api.uninstallApp",
    ]
    for other in ("send(", "fetch(", "'POST'", "'PUT'", "'DELETE'", "localStorage", "'/api/"):
        assert other not in section, other
    assert "appFile:      (id, name)     => send('/api/app/'+enc(id)+'/file?name='+enc(name), 'GET')," in page
    assert "uninstallApp: id             => send('/api/apps/'+enc(id), 'DELETE')," in page

    # The popup's one write is the install, through the API block.
    section = page[popup:home]
    assert re.findall(r"api\.\w+", section) == ["api.installApp"]
    for other in ("send(", "fetch(", "'POST'", "'PUT'", "'DELETE'", "localStorage", "'/api/"):
        assert other not in section, other
    assert "installApp:   files          => send('/api/apps', 'POST', {files})," in page


def test_add_application_builds_the_app_from_file_slots(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    popup = _js_function(page, "openAddApp")

    # The slots: app.yaml required, app.py optional, and Add file for the
    # rest, each named.
    assert "fileSlot('app.yaml', true, say)" in popup
    assert "fileSlot('app.py', false, say)" in popup
    assert "act('add-file').onclick" in popup and "fileSlot('', false, say)" in popup
    assert '>＋ Add file</button>' in popup
    # Each slot is a paste box with Upload beside it, filling the same box.
    slot = _js_function(page, "fileSlot")
    assert "<textarea" in slot and '<input type="file" hidden>' in slot
    assert 'data-act="upload"' in slot and "picker.click()" in slot
    assert "text.value = await file.text();" in slot
    assert '<input class="input fname"' in slot and 'data-act="remove"' in slot
    assert "${esc(fixed)}" in slot

    # Review reads the id from the app.yaml - there's no id field - and
    # names it, or says why it can't.
    assert "manifestId(files.find(x => x.name === 'app.yaml').text)" in popup
    assert "idLine.innerHTML = `Install application: <code>${esc(read.id)}</code>`;" in popup
    assert "else idLine.textContent = read.problem;" in popup
    assert "install.disabled = !read.id;" in popup
    assert "data-key" not in popup and "id-field" not in popup
    # The files to install, listed - every name escaped.
    assert "`<li><code>${esc(x.name)}</code></li>`" in popup

    # The custom-code notice: on Review, only when a file is Python.
    assert (
        "const CUSTOM_CODE = 'This app includes custom code and runs with full "
        "access to your OpenFit data.';"
    ) in page
    assert '<p class="install-warn" data-f="notice" hidden>${esc(CUSTOM_CODE)}</p>' in popup
    assert "f('notice').hidden = !files.some(x => /\\.py$/i.test(x.name));" in popup

    # Install sends the files; success closes the popup and re-reads the
    # list and Home at once - an error keeps it open, the server's message
    # shown as given.
    assert "const res = await api.installApp(body);" in popup
    assert "close();\n        appInstalled(res);" in popup
    assert "say(res.error || 'Could not install this application');" in popup
    after = _js_function(page, "appInstalled")
    assert "loadHome();" in after and "const listed = await loadApps();" in after
    assert "appsNote('bad'," in after and "appsNote('ok'," in after
    # loadApps hands back what it read, so the popup can tell whether the
    # new app loaded.
    assert "return read;" in _js_function(page, "loadApps")

    # The page's id rule is the server's.
    js_id = re.search(r"const INSTALL_ID = /\^(.*)\$/;", page).group(1)
    assert js_id + r"\Z" == apps.INSTALL_ID.pattern


# Names and app.yamls the popup checks before Install, as the server will.
MIRRORED_NAMES = [
    "app.yaml", "app.py", "thresholds.yaml", "a.b.c", ".env", "with space.txt", "ünï.txt",
    "../evil", "a/b.py", "a\\b.py", "", ".", "..", "...", "a..b", "/etc/passwd", "sub/",
    "nul\x00.py", "line\nbreak.py", "tab\t.py", "del\x7f.py",
]
MIRRORED_YAMLS = [
    "id: steps\nname: Steps\n",
    "name: Steps\nid: steps-2\n",
    "id: 'quoted'\n",
    'id: "double-quoted"\n',
    "id: steps # a comment\n",
    "---\nid: after-a-marker\n",
    "id: steps\r\nname: CRLF\r\n",
    "id: first\nid: last\n",
    "id:\nname: Null\n",
    "name: No id\n",
    "settings:\n  id: nested\n",
    "",
    "   \n",
    "id: My-App\n",
    "id: a_b\n",
    "id: -x\n",
    "id: 'unclosed\n",
    "id: ''\n",
]


def _server_id(text):
    try:
        return apps._install_id(apps._manifest_to_install({"app.yaml": text}))
    except apps.InstallError:
        return None


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_popups_checks_mirror_the_servers(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    # The page's own checks, run as written: what they let through, the
    # server takes, and what they refuse, it refuses.
    source = "\n".join([
        re.search(r"\n  (const INSTALL_ID = .*?;)\n", page).group(1),
        re.search(r"\n  (const NO_ID = .*?;)\n", page).group(1),
        _js_source(page, "fileNameProblem"),
        _js_source(page, "manifestId"),
    ])
    script = source + f"""
        const names = {json.dumps(MIRRORED_NAMES)}, yamls = {json.dumps(MIRRORED_YAMLS)};
        console.log(JSON.stringify({{names: names.map(fileNameProblem), yamls: yamls.map(manifestId)}}));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)

    for name, problem in zip(MIRRORED_NAMES, result["names"]):
        assert bool(problem) == bool(apps.filename_problem(name)), repr(name)
    for text, read in zip(MIRRORED_YAMLS, result["yamls"]):
        assert read.get("id") == _server_id(text), repr(text)
        assert bool(read.get("problem")) == (_server_id(text) is None), repr(text)


def test_an_app_row_expands_as_a_data_row_does(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    # One mechanism for both lists: Your data's rows and the app rows are
    # each an item and a head handed to expandable(), which owns the
    # click, the chevron (.open) and aria-expanded.
    data_row = _js_function(page, "rowItem")
    app_row = _js_function(page, "appRow")
    assert "return expandable(item, head, {" in data_row
    assert "open:state.open, id:row.id, panelId:panelId(row)," in data_row
    assert "return expandable(item, head, {" in app_row
    assert "open:appsList.open, id:a.id, panelId:appPanelId(a), panel:() => appPanel(a)," in app_row
    for row in (data_row, app_row):
        assert ".onclick" not in row and "aria-expanded" not in row
    # An open app row stays open across a refresh, as a data row does.
    assert "open:new Set(),     // ids of expanded rows" in page
    # The same look: a .ditem holding a .drow head with a chevron, its
    # first column the data grid's, and the detail a .dpanel.
    assert "el('div','ditem aitem')" in app_row
    assert '<span class="chev" aria-hidden="true">›</span><span class="a-name">' in app_row
    assert ".arow{display:grid;grid-template-columns:16px minmax(0,1fr);" in page
    assert "el('div','dpanel apanel'," in _js_function(page, "appPanel")
    # One column on a phone, as the data rows' panels are.
    assert ".dpanel, .dpanel.apanel{grid-template-columns:1fr;" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_expandable_opens_closes_and_restores_a_row(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    # expandable() run as written, on stand-in elements.
    script = _js_source(page, "expandable") + """
        const mk = () => {
          const n = {children:[], attrs:{}, cls:new Set(), id:''};
          n.classList = {toggle:(c, on) => { if(on) n.cls.add(c); else n.cls.delete(c); }};
          n.setAttribute = (k, v) => { n.attrs[k] = String(v); };
          n.appendChild = c => { n.children.push(c); c.parent = n; return c; };
          n.remove = () => { const s = n.parent.children; s.splice(s.indexOf(n), 1); };
          return n;
        };
        const seen = [];
        const look = (item, head) => seen.push(
          [item.cls.has('open'), head.attrs['aria-expanded'], item.children.length]);
        const open = new Set();
        let built = 0, closed = 0;
        const opts = id => ({open, id, panelId:'p-'+id,
          panel:() => { built++; const p = mk(); p.id = 'p-'+id; return p; },
          onClose:() => { closed++; }});
        const item = mk(), head = mk();
        expandable(item, head, opts('a'));
        look(item, head);
        head.onclick(); look(item, head);
        head.onclick(); look(item, head);
        head.onclick();
        // Repainted while open (a refresh): it comes back open.
        const again = mk(), head2 = mk();
        expandable(again, head2, opts('a'));
        look(again, head2);
        console.log(JSON.stringify({seen, built, closed, open:[...open],
          type:head.type, controls:head.attrs['aria-controls']}));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "seen": [[False, "false", 1], [True, "true", 2], [False, "false", 1], [True, "true", 2]],
        "built": 3,
        "closed": 1,
        "open": ["a"],
        "type": "button",
        "controls": "p-a",
    }


def test_an_open_app_row_shows_reads_files_and_a_writes_slot(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    panel = _js_function(page, "appPanel")
    # Reads: the manifest's keys, escaped - or a muted note.
    assert "<h5>Reads</h5>" in panel
    assert '`<li class="akey" data-tip="${esc(metricName(k))}">${esc(k)}</li>`' in panel
    assert "'<p class=\"dnote\">No declared reads</p>'" in panel
    # Files: each a button opening the viewer on that file.
    assert "<h5>Files</h5>" in panel
    assert 'data-file="${i}" aria-haspopup="dialog">${esc(f)}</button>' in panel
    assert "b.onclick = () => openAppFile(a, files[+b.dataset.file]);" in panel
    # Writes: a reserved slot, marked for the write-back follow-up - and
    # nothing counted, so nothing read.
    assert "<div>${writesTicker()}</div>" in panel
    assert "// writes-ticker: filled in when app-tier write-back exists\n  function writesTicker(){" in page
    ticker = _js_function(page, "writesTicker")
    assert "<h5>Writes</h5>" in ticker and "—" in ticker
    for read in ("api.", "await", "fetch("):
        assert read not in ticker, read
    # The actions: Refresh, and Uninstall in the danger style.
    assert '<button class="btn sm" data-act="refresh" type="button">Refresh</button>' in panel
    assert '<button class="btn sm danger" data-act="uninstall" type="button">Uninstall</button>' in panel


def test_a_file_opens_read_only_in_the_viewer(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    viewer = _js_function(page, "openAppFile")
    # A popup, reading the file from the file route ...
    assert "openModal(`" in viewer
    assert "api.appFile(a.id, name).then(res => {" in viewer
    # ... into a monospace block, as text - never markup, nothing to edit.
    assert '<pre class="file-view" tabindex="0" aria-label="${esc(name)}">' in viewer
    assert "view.textContent = res.text;" in viewer
    assert "innerHTML" not in viewer
    for editable in ("<textarea", "<input", "contenteditable"):
        assert editable not in viewer, editable
    assert "${esc(name)}" in viewer and "${esc(a.name || a.id)}" in viewer
    # The server's refusal shown as given.
    assert "(res.error || \"Couldn't read this file.\")" in viewer
    assert re.search(r"^\.file-view\{[^}]*font-family:var\(--font-mono\)", page, re.M)


def test_uninstall_confirms_then_deletes_and_rereads_the_list_and_home(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    panel = _js_function(page, "appPanel")
    # Uninstall only opens the confirm - a danger panel naming the app.
    assert "act('uninstall').onclick = () => gateOpen(true);" in panel
    assert '<div class="danger-panel a-confirm" hidden>' in panel
    assert "<h5>Uninstall ${esc(name)}?</h5>" in panel
    assert "<p>This deletes its files from <code>/data/apps/</code>.</p>" in panel
    assert "act('cancel').onclick = () => gateOpen(false);" in panel
    # The confirm's button is the one that DELETEs ...
    confirm = panel[panel.index("confirmBtn.onclick = async () => {") :]
    assert panel.count("api.uninstallApp(") == 1
    assert "const res = await api.uninstallApp(a.id);" in confirm
    # ... and then the app leaves both the list and Home; an error stays.
    done = confirm[confirm.index("if(res.ok){") : confirm.index("}else{")]
    assert "loadApps();" in done and "loadHome();" in done
    assert "appsList.open.delete(a.id);" in done
    assert "say('bad', res.error || 'Could not uninstall this application');" in confirm
    # No confirm(): the panel is the gate, as Remove source's is.
    assert "confirm(" not in panel


def test_refresh_reruns_the_render_and_redraws_the_tile(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    panel = _js_function(page, "appPanel")
    refresh = panel[panel.index("refresh.onclick = async () => {") : panel.index("const gateOpen")]
    # The existing render route, re-run - no new endpoint, no rescan, no
    # page reload - and its Home tile redrawn from the fresh spec.
    assert "spec = await api.renderApp(a.id);" in refresh
    assert "repaintTile(a.id, spec);" in refresh
    for other in ("loadApps", "loadHome", "location.reload"):
        assert other not in refresh, other
    tile = _js_function(page, "repaintTile")
    assert "home.tiles.find(x => x.app_id===appId)" in tile
    assert "t.spec = spec;" in tile
    assert "tile.replaceChildren(renderSpec(spec, t.name));" in tile
    # Mid-Configure, the tile keeps its handle and picker.
    assert "if(controls) tile.appendChild(controls);" in tile


def _page(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    return page


def _home(page):
    """The page's Home section: its script, from its banner to the spec renderer."""
    start = page.index("HOME - the installed apps' tiles")
    return page[start : page.index("---- spec -> DOM: the display vocabulary ----", start)]


def _const(page, name):
    """A top-level const in the page's script, without its trailing comment."""
    m = re.search(rf"\n  (const {name} = .*?;)(?: +//[^\n]*)?\n", page)
    assert m, name
    return m.group(1)


def test_home_lays_its_tiles_on_a_six_cell_grid_of_squares(client):
    page = _page(client)
    # Six columns of square cells - each row as tall as a column is wide -
    # the cell Home's width over six, held between a floor and a ceiling
    # (both in the token block); past the ceiling the grid centres.
    tiles = re.search(r"^\.tiles\{([^}]*)\}", page, re.M).group(1)
    assert "--tile-cell:clamp(var(--tile-cell-min), (100cqi - 5 * var(--tile-gap)) / 6, var(--tile-cell-max));" in tiles
    assert "grid-template-columns:repeat(6, var(--tile-cell));" in tiles
    assert "grid-auto-rows:var(--tile-cell);" in tiles
    assert "width:max-content;margin:0 auto;" in tiles
    # Row flow, never dense: a gap at a row's end is never backfilled.
    assert "grid-auto-flow:row;" in tiles
    assert not re.search(r"grid-auto-flow:[^;]*dense", page)
    tokens = page[page.index("CUSTOMISE OPENFIT HERE") : page.index("@media (prefers-color-scheme:dark)")]
    for token in ("--tile-cell-min:", "--tile-cell-max:", "--tile-gap:", "--tile-guide:"):
        assert token in tokens, token
    # Home's width is what the cell is measured against; under the floor
    # it scrolls sideways rather than squeeze the cells (no phone pass yet).
    assert re.search(r"^\.home\{container-type:inline-size;overflow-x:auto;", page, re.M)
    # The page's grid is the server's: six cells, the most a tile spans.
    assert _const(page, "GRID_COLS") == "const GRID_COLS = 6;"
    assert apps.MAX_CELLS == 6

    # Each tile spans its size, from wherever the flow puts it: no start cell.
    span = _js_function(page, "spanTile")
    assert "tile.style.gridColumn = 'span '+t.w;" in span
    assert "tile.style.gridRow    = 'span '+t.h;" in span
    assert "/ '" not in span and "t.x" not in span and "t.y" not in span


def test_home_has_a_sub_header_with_the_subtitle_and_configure(client):
    page = _page(client)
    # Under the tab strip: the sub-header, its message line, then the tiles.
    assert re.search(
        r'<section data-panel="home" aria-label="Home" hidden>\s*'
        r'<div class="home-head" id="homeHead"></div>\s*'
        r'<p class="form-msg home-msg" id="homeMsg" role="status"></p>\s*'
        r'<div class="home" id="home"></div>\s*</section>',
        page,
    )
    boot = page[page.index("BOOT - paint the shell") :]
    assert "paintHomeHead();" in boot and "loadHome();" in boot

    head = _js_function(page, "paintHomeHead")
    # The subtitle: text in view, an input while configuring - set as
    # text either way, never markup.
    assert "if(home.configuring){" in head
    assert "input.value = home.subtitle;" in head
    assert "sub.textContent = home.subtitle;" in head
    assert "input.maxLength = SUBTITLE_MAX;" in head
    assert "input.oninput = () => { home.subtitle = input.value; };" in head
    assert "${" not in head and "innerHTML" not in head
    assert _const(page, "SUBTITLE_MAX") == f"const SUBTITLE_MAX = {home_layout.MAX_SUBTITLE};"
    # Configure: a tab like ＋ Add, on the right, a toggle - pressed and
    # filled while on; never a tab of the strip (no data-tab), so it isn't
    # an Opening page.
    assert "el('button','tab tab-add home-cfg' + (home.configuring ? ' active' : ''), '<span>Configure</span>');" in head
    assert "cfg.setAttribute('aria-pressed', home.configuring ? 'true' : 'false');" in head
    assert "cfg.onclick = toggleConfigure;" in head
    assert "data-tab" not in head
    assert ".tab.home-cfg.active, .tab.home-cfg.active:hover{background:var(--accent);" in page


def test_configure_shows_controls_only_while_on_and_a_picker_only_for_a_choice(client):
    page = _page(client)
    paint = _js_function(page, "paintTiles")
    assert "el('div','tiles' + (configuring ? ' configuring' : ''));" in paint
    assert "if(configuring) tile.appendChild(tileControls(t, tile));" in paint
    assert "if(configuring) sortableTiles(grid);" in paint
    assert "paintTiles(box, home.tiles, home.configuring);" in _js_function(page, "paintHome")
    controls = _js_function(page, "tileControls")
    assert "if(t.sizes.length > 1){" in controls
    # The app's own sizes, as given - the page never makes one up - and
    # its name only ever as text.
    assert "t.sizes.forEach((s, i) => {" in controls
    assert "o.textContent = s.w+' × '+s.h;" in controls
    assert "${" not in controls and "innerHTML" not in controls
    # Dragging moves only the order: home.tiles follows the DOM.
    sortable = _js_function(page, "sortableTiles")
    assert "home.tiles.sort((a, b) => order.indexOf(a.app_id) - order.indexOf(b.app_id));" in sortable
    assert "grid.insertBefore(drag.tile, spot.before);" in sortable
    assert "e.key==='ArrowLeft' || e.key==='ArrowUp'" in sortable


def test_home_reads_its_layout_and_writes_it_only_through_configure(client):
    page = _page(client)
    home = _home(page)
    # Through the API block, the only place that knows a URL.
    assert sorted(set(re.findall(r"api\.\w+", home))) == [
        "api.apps", "api.homeLayout", "api.renderApp", "api.saveLayout",
    ]
    for other in ("send(", "fetch(", "'POST'", "'PUT'", "'DELETE'", "localStorage", "'/api/"):
        assert other not in home, other
    assert "homeLayout:   ()             => fetchJSON('/api/home-layout')," in page
    assert "saveLayout:   layout         => send('/api/home-layout', 'PUT', layout)," in page
    # Saved once, as Configure is switched off: every tab, the one on
    # screen with its subtitle and its instances' order, sizes, ids and
    # configs.
    toggle = _js_function(page, "toggleConfigure")
    assert home.count("api.saveLayout(") == 1
    assert "const res = await api.saveLayout(layoutToSave());" in toggle
    assert _const(page, "layoutItems") == (
        "const layoutItems = () => home.tiles.map(t => ({instance_id:t.instance_id, app_id:t.app_id, "
        "size:{w:t.w, h:t.h}, config:t.config || {}}));"
    )
    save = _js_function(page, "layoutToSave")
    assert (
        "tabs:home.layout.tabs.map(t => t.id===home.activeTab ? "
        "{...t, subtitle:home.subtitle, items:layoutItems()} : t)," in save
    )
    assert "default_tab:home.layout.default_tab," in save


def _node(script):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# Stand-in elements for running the page's own functions under node.
FAKE_DOM = """
    const mk = tag => {
      const n = {tag, children:[], style:{}, dataset:{}, attrs:{}, className:''};
      n.setAttribute = (k, v) => { n.attrs[k] = String(v); };
      n.appendChild = c => { n.children.push(c); return c; };
      n.replaceChildren = (...cs) => { n.children = cs; };
      return n;
    };
    const el = (t, c, h) => { const n = mk(t); if(c) n.className = c; if(h!=null) n.innerHTML = h; return n; };
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_paint_tiles_spans_each_tile_and_configure_adds_its_controls(client):
    page = _page(client)
    script = FAKE_DOM + "\n".join([
        _const(page, "GRIP"),
        _js_source(page, "spanTile"),
        _js_source(page, "tileControls"),
        _js_source(page, "paintTiles"),
    ]) + """
        const renderSpec = (spec, name) => ({tag:'face', spec, name, className:'face'});
        let sortable = 0;
        const sortableTiles = grid => { sortable++; };
        const tiles = [
          {app_id:'week', name:'Week <b>', spec:{type:'empty'}, w:2, h:1,
           sizes:[{w:2, h:1}, {w:1, h:1}, {w:3, h:2}]},
          {app_id:'steps', name:'Steps', spec:{type:'empty'}, w:1, h:1, sizes:[{w:1, h:1}]},
        ];
        const look = grid => ({cls:grid.className, tiles:grid.children.map(t => ({
          app:t.dataset.app, cls:t.className, label:t.attrs['aria-label'],
          col:t.style.gridColumn, row:t.style.gridRow,
          parts:t.children.map(c => c.className),
          controls:(t.children.find(c => c.className==='tile-cfg') || {children:[]}).children.map(c => ({
            cls:c.className, label:c.attrs['aria-label'],
            options:c.children.map(o => [o.value, o.textContent, o.selected])})),
        }))});
        const box = mk('div');
        paintTiles(box, tiles, false);
        const view = look(box.children[0]);
        const sortableInView = sortable;
        paintTiles(box, tiles, true);
        const grid = box.children[0];
        const configuring = look(grid);
        // Pick week's 3 x 2: the tile and its entry both change.
        const pick = grid.children[0].children[1].children[1];
        pick.value = '2';
        pick.onchange();
        console.log(JSON.stringify({view, sortableInView, configuring, sortable,
          picked:[grid.children[0].style.gridColumn, grid.children[0].style.gridRow, tiles[0].w, tiles[0].h]}));
    """
    result = _node(script)

    # View: a plain grid of chromeless tiles, each spanning its size.
    assert result["view"] == {"cls": "tiles", "tiles": [
        {"app": "week", "cls": "tile", "label": "Week <b>", "col": "span 2", "row": "span 1",
         "parts": ["face"], "controls": []},
        {"app": "steps", "cls": "tile", "label": "Steps", "col": "span 1", "row": "span 1",
         "parts": ["face"], "controls": []},
    ]}
    assert result["sortableInView"] == 0
    # Configure: every tile its handle; only the app with a choice of sizes
    # a picker, its sizes in the app's order, the current one selected.
    week, steps = result["configuring"]["tiles"]
    assert result["configuring"]["cls"] == "tiles configuring"
    assert week["parts"] == ["face", "tile-cfg"] and steps["parts"] == ["face", "tile-cfg"]
    assert week["controls"] == [
        {"cls": "tile-grip", "label": "Move Week <b>: drag it, or use the arrow keys", "options": []},
        {"cls": "tile-size", "label": "Size of Week <b>",
         "options": [["0", "2 × 1", True], ["1", "1 × 1", False], ["2", "3 × 2", False]]},
    ]
    assert steps["controls"] == [
        {"cls": "tile-grip", "label": "Move Steps: drag it, or use the arrow keys", "options": []},
    ]
    assert result["sortable"] == 1
    assert result["picked"] == ["span 3", "span 2", 3, 2]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_configure_saves_the_layout_as_its_switched_off(client):
    page = _page(client)
    script = "\n".join([
        _const(page, "layoutItems"),
        _js_source(page, "layoutToSave"),
        _js_source(page, "toggleConfigure"),
    ]) + """
        // The tab on screen and one more: its tiles are what's saved of it
        // (its items as read are stale), the other goes back as read. b's
        // tile was placed by the page, so it has no instance id yet.
        const home = {status:'ready', subtitle:'Mine', configuring:false, saving:false, activeTab:'home',
          layout:{default_tab:'home', tabs:[
            {id:'home', name:'Home', mode:'flow', subtitle:'Mine', items:[]},
            {id:'two', name:'Two', mode:'flow', subtitle:'Other',
             items:[{instance_id:'t1', app_id:'a', size:{w:1, h:1}, config:{k:1}}]},
          ]},
          tiles:[{instance_id:'i1', app_id:'a', w:2, h:1, spec:{}, config:{x:1}},
                 {app_id:'b', w:1, h:1, spec:{}, config:{}}]};
        const saved = [];
        let reply = {ok:true};
        const api = {saveLayout: async layout => { saved.push(JSON.parse(JSON.stringify(layout))); return reply; }};
        const notes = [];
        const homeMsg = (cls, text) => notes.push(cls);
        const paintHomeHead = () => {}, paintHome = () => {};
        const $ = () => ({setAttribute(){}});
        (async () => {
          const seen = [];
          await toggleConfigure(); seen.push(home.configuring);
          home.tiles.reverse(); home.tiles[1].w = 3; home.subtitle = 'Edited';
          await toggleConfigure(); seen.push(home.configuring);
          await toggleConfigure(); seen.push(home.configuring);
          reply = {ok:false, error:'disk full'};
          await toggleConfigure(); seen.push(home.configuring);
          console.log(JSON.stringify({seen, saved, notes, saving:home.saving}));
        })();
    """
    result = _node(script)

    # On, then off (saved), on, then a failed save: still configuring.
    assert result["seen"] == [True, False, True, True]
    assert result["saving"] is False
    assert result["notes"] == ["info", "info", "ok", "info", "info", "bad"]
    # Nothing is saved going in; going out, the layout as it stands - the
    # tab on screen as edited, b's instance with no id, the other tab as read.
    layout = {"default_tab": "home", "tabs": [
        {"id": "home", "name": "Home", "mode": "flow", "subtitle": "Edited", "items": [
            {"app_id": "b", "size": {"w": 1, "h": 1}, "config": {}},
            {"instance_id": "i1", "app_id": "a", "size": {"w": 3, "h": 1}, "config": {"x": 1}},
        ]},
        {"id": "two", "name": "Two", "mode": "flow", "subtitle": "Other", "items": [
            {"instance_id": "t1", "app_id": "a", "size": {"w": 1, "h": 1}, "config": {"k": 1}},
        ]},
    ]}
    assert result["saved"] == [layout, layout]
    # ... which is what the server takes, minting b's instance an id.
    parsed = home_layout.parse_layout(layout)
    assert home_layout.INSTANCE_ID.match(parsed["tabs"][0]["items"][0].pop("instance_id"))
    assert parsed == layout


# Installed apps (as /api/apps lists them) and stored items, for the
# page's flow and the server's reconcile to square the same way.
FLOW_APPS = [
    {"id": "a", "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 2}]},
    {"id": "b", "sizes": [{"w": 3, "h": 1}]},
    {"id": "c", "sizes": [{"w": 2, "h": 1}, {"w": 1, "h": 1}, {"w": 6, "h": 6}]},
    {"id": "d", "sizes": [{"w": 1, "h": 2}]},
]
FLOW_ITEMS = [
    [],
    None,
    [{"app_id": "c", "size": {"w": 6, "h": 6}}, {"app_id": "a", "size": {"w": 2, "h": 2}}],
    [{"app_id": "gone", "size": {"w": 1, "h": 1}}, {"app_id": "b", "size": {"w": 3, "h": 1}}],
    [{"app_id": "a", "size": {"w": 3, "h": 3}}, {"app_id": "b", "size": {"w": 1, "h": 1}}],
    [{"app_id": "d"}, {"app_id": "a", "size": None}, {"app_id": "c", "size": [1, 1]}],
    [{"app_id": "a", "size": {"w": 2, "h": 2}}, {"app_id": "a", "size": {"w": 1, "h": 1}}],
    ["a", 5, None, {"app_id": 5}, {"app_id": ["a"]}, {"size": {"w": 1, "h": 1}}],
    [{"app_id": "c", "size": {"w": True, "h": 1}}, {"app_id": "c", "size": {"w": "1", "h": "1"}}],
    [{"app_id": "d", "size": {"w": 1, "h": 2, "x": 4}}, {"app_id": "b", "size": {"w": 3, "h": 1}}],
]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_pages_flow_squares_a_layout_as_the_server_does(client):
    page = _page(client)
    listed = [{**a, "name": a["id"], "default_size": a["sizes"][0]} for a in FLOW_APPS]
    script = "\n".join([
        _const(page, "GRID_COLS"),
        _const(page, "cells"),
        _const(page, "sizeOf"),
        _const(page, "appSizes"),
        _js_source(page, "flowLayout"),
    ]) + f"""
        const apps = {json.dumps(listed)}, cases = {json.dumps(FLOW_ITEMS)};
        console.log(JSON.stringify(cases.map(items => flowLayout(items, apps))));
    """
    result = _node(script)

    installed = [
        apps.InstalledApp(id=a["id"], name=a["id"], sizes=a["sizes"], default_size=a["sizes"][0],
                          reads=[], config=[], path="/nowhere", render=None)
        for a in FLOW_APPS
    ]
    for items, flowed in zip(FLOW_ITEMS, result):
        # Items from before tabs: the server mints their ids for the read,
        # and the page, with none to carry, leaves them for the save.
        squared = home_layout.reconcile({"items": items}, installed)["tabs"][0]["items"]
        assert flowed == [{"app_id": i["app_id"], "size": i["size"], "config": {}} for i in squared], items


def _instance(instance_id, app_id, w=1, h=1, config=None):
    return {"instance_id": instance_id, "app_id": app_id, "size": {"w": w, "h": h}, "config": config or {}}


# The tab on screen and the other tab's instances, as the server hands
# them out, for the page and the server to square the same way. (Not a
# tab placing one app twice: the server keeps both, the page - its tiles
# still one an app - the first.)
FLOW_TABS = [
    ([], []),
    ([_instance("c1", "c", 6, 6, {"goal": 5}), _instance("a1", "a", 2, 2)], []),
    # a is on the other tab: placed, so not added to this one.
    ([_instance("g1", "gone"), _instance("b1", "b", 3, 1)], [_instance("a2", "a")]),
    ([_instance("a1", "a", 3, 3)], [_instance("d2", "d", 1, 2), _instance("c2", "c")]),
    ([], [_instance("g2", "gone")]),
    ([_instance("b1", "b", 3, 1, {"x": [1]})], [_instance("b2", "b", 3, 1)]),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_pages_flow_squares_a_tab_as_the_server_does(client):
    page = _page(client)
    listed = [{**a, "name": a["id"], "default_size": a["sizes"][0]} for a in FLOW_APPS]
    script = "\n".join([
        _const(page, "GRID_COLS"),
        _const(page, "cells"),
        _const(page, "sizeOf"),
        _const(page, "appSizes"),
        _js_source(page, "flowLayout"),
    ]) + f"""
        const apps = {json.dumps(listed)}, cases = {json.dumps(FLOW_TABS)};
        console.log(JSON.stringify(cases.map(([shown, other]) =>
          flowLayout(shown, apps, other.map(it => it.app_id)))));
    """
    result = _node(script)

    installed = [
        apps.InstalledApp(id=a["id"], name=a["id"], sizes=a["sizes"], default_size=a["sizes"][0],
                          reads=[], config=[], path="/nowhere", render=None)
        for a in FLOW_APPS
    ]
    for (shown, other), flowed in zip(FLOW_TABS, result):
        stored = {"tabs": [
            {"id": "home", "name": "Home", "mode": "flow", "subtitle": "", "items": shown},
            {"id": "two", "name": "Two", "mode": "flow", "subtitle": "", "items": other},
        ], "default_tab": "home"}
        squared = home_layout.reconcile(stored, installed)["tabs"][0]["items"]
        # Same instances, ids and configs; an app placed nowhere goes on the
        # end with an id the server mints, and none yet on the page.
        given = {i["instance_id"] for i in shown + other}
        assert flowed == [
            i if i["instance_id"] in given else {k: v for k, v in i.items() if k != "instance_id"}
            for i in squared
        ], (shown, other)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_dragged_tile_takes_the_place_of_the_one_its_dropped_on(client):
    page = _page(client)
    # Four tiles on 100px cells: a (2x1) and b (1x1) in row one, three
    # cells left empty at its end; c (3x1) and d (1x1) in row two.
    script = _js_source(page, "dropSpot") + """
        const rect = (l, t, r, b) => ({left:l, top:t, right:r, bottom:b, width:r-l, height:b-t});
        const tiles = [['a', 0, 0, 200, 100], ['b', 200, 0, 300, 100], ['c', 0, 100, 300, 200], ['d', 300, 100, 400, 200]]
          .map(([id, ...r]) => ({dataset:{app:id}, getBoundingClientRect:() => rect(...r)}));
        tiles.forEach((t, i) => Object.defineProperty(t, 'nextElementSibling', {get:() => tiles[i+1] || null}));
        const grid = {children:tiles};
        const by = id => tiles.find(t => t.dataset.app===id);
        const at = (id, x, y) => {
          const spot = dropSpot(grid, by(id), x, y);
          return spot && [spot.before ? spot.before.dataset.app : null, spot.key];
        };
        console.log(JSON.stringify({
          backOntoA: at('d', 50, 50),        // from later: in front of a
          forwardOntoC: at('a', 250, 150),   // from earlier: after c
          forwardOntoB: at('a', 210, 10),    // after b, whichever half
          backOntoC: at('d', 10, 190),
          rowEndGap: at('a', 550, 50),       // the gap at row one's end: after b
          gapOfItsOwnRow: at('c', 550, 50),  // c is after b already
          rowTwoGap: at('a', 450, 150),      // after d, the last
          dropRow: at('b', 50, 250),         // under every tile: last
          overItself: at('b', 250, 50),
        }));
    """
    assert _node(script) == {
        "backOntoA": ["a", "a"],
        "forwardOntoC": ["d", "c"],
        "forwardOntoB": ["c", "b"],
        "backOntoC": ["c", "c"],
        "rowEndGap": ["c", ":gap:b"],
        "gapOfItsOwnRow": ["d", ":gap:b"],
        "rowTwoGap": [None, ":gap:d"],
        "dropRow": [None, ":end"],
        "overItself": None,
    }


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
