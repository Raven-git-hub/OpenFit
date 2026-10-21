"""API route tests - the shape the frontend depends on."""

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime

import pytest
import yaml

import apps
import home_layout
from derived import recompute_derived


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
    for spec in ("number:", "line:", "panel:", "input:", "empty:", "error:"):
        assert spec in page
    # The layout is tabs, each a flow - an order and a size per tile -
    # read with the apps and a spec each (the fetch), kept, and drawn a
    # tab at a time from memory (the render), squared with the apps.
    assert "const layoutTiles = flowLayout;" in page
    assert "autoLayout" not in page
    load = _js_function(page, "loadHome")
    assert "await Promise.all([api.apps(), api.homeLayout()]);" in load
    assert "home.specs = new Map(apps.map((a, i) => [a.id, {a, spec:specs[i]}]));" in load
    assert "home.layout = saved;" in load and "if(read) renderActiveTab();" in load
    render = _js_function(page, "renderActiveTab")
    assert "api." not in render
    assert (
        "const tab = tabs.find(t => t.id===home.activeTab) || "
        "tabs.find(t => t.id===home.layout.default_tab) || tabs[0];" in render
    )
    assert "home.activeTab = tab.id;" in render
    assert "home.tiles = layoutTiles(tab.items, apps, elsewhere).map(it => {" in render
    # Each tile carries its instance's id and config, for the save.
    assert "return {instance_id:it.instance_id, app_id:a.id, name:a.name, spec," in render
    assert "w:it.size.w, h:it.size.h, sizes:appSizes(a), config:it.config};" in render


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


def test_your_data_groups_in_the_vocabularys_category_order(client):
    # The page's own copy of CATEGORIES (app/metrics.py), Roster included:
    # a category it left out would still be listed, but after the rest.
    from metrics import CATEGORIES

    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    order = re.search(r"const CATEGORY_ORDER = \[(.*?)\];", page, re.S)
    assert order, "CATEGORY_ORDER"
    # Each a JS string, in single quotes or - Women's health - double.
    names = [single or double for single, double in re.findall(r"'([^']*)'|\"([^\"]*)\"", order.group(1))]
    assert names == list(CATEGORIES)


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
    # the eight real apps.
    fixtures = os.path.join(os.path.dirname(__file__), "fixtures", "apps")
    monkeypatch.setitem(main.app.config, "APPS_DIR", fixtures)
    apps = client.get("/api/apps").get_json()
    assert [(a["id"], a["name"]) for a in apps] == [
        ("caffeine", "Caffeine"), ("frms", "FRMS"), ("import", "Import weigh-ins"), ("mood", "Mood"),
        ("steps", "Steps"), ("steps-metres", "Distance (from steps)"), ("weight", "Weight"),
        ("workout-log", "Workout log"),
    ]

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
    # ... except Add Application, which opens the install popup - empty:
    # the click's event isn't handed on as a prefill.
    assert "add.onclick = () => openAddApp();" in actions
    # The card's one line of news, for what an install did.
    assert "note.id = 'appsMsg';" in controls


def test_the_applications_list_writes_only_uninstall_and_the_popup_installs_or_updates(client):
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

    # The popup's one write is the install - or, opened by Update, the
    # update - through the API block.
    section = page[popup:home]
    assert re.findall(r"api\.\w+", section) == ["api.updateApp", "api.installApp"]
    for other in ("send(", "fetch(", "'POST'", "'PUT'", "'DELETE'", "localStorage", "'/api/"):
        assert other not in section, other
    assert "installApp:   files          => send('/api/apps', 'POST', {files})," in page
    assert "updateApp:    (id, files)    => send('/api/apps/'+enc(id), 'PUT', {files})," in page


def test_add_application_builds_the_app_from_file_slots(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()
    popup = _js_function(page, "openAddApp")

    # The slots: app.yaml required, app.py optional, and Add file for the
    # rest, each named.
    assert "fileSlot('app.yaml', true, say, prefilled('app.yaml'))" in popup
    assert "fileSlot('app.py', false, say, prefilled('app.py'))" in popup
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
    assert "const yamlText = files.find(x => x.name === 'app.yaml').text;" in popup
    assert "const read = update ? updateId(yamlText, update.id) : manifestId(yamlText);" in popup
    assert "const verb = update ? 'Update' : 'Install';" in popup
    assert "idLine.innerHTML = `${verb} application: <code>${esc(read.id)}</code>`;" in popup
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

    # Install sends the files; success closes the popup, puts the app on
    # the tab picked (if one was), and re-reads the list and Home at once -
    # an error keeps it open, the server's message shown as given.
    assert "const res = update ? await api.updateApp(update.id, body) : await api.installApp(body);" in popup
    assert (
        "close();\n        if(tabId) await placeNewAppOnTab(res.id, tabId);\n"
        "        appInstalled(res, update ? 'Updated' : 'Installed');"
    ) in popup
    assert "say(res.error || `Could not ${verb.toLowerCase()} this application`);" in popup
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
        _js_source(page, "yamlId"),
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
    # Reads: the manifest's keys, escaped - or a muted note. A key the app
    # invents is named by its declared label, any other by metricName().
    assert "<h5>Reads</h5>" in panel
    assert '`<li class="akey" data-tip="${esc(readName(k))}">${esc(k)}</li>`' in panel
    assert "const readName = k => (own.find(m => m.key===k) || {}).label || metricName(k);" in panel
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
    # With its tile's config: the one on screen, else its instance's.
    assert "spec = await api.renderApp(a.id, tileCfg(a.id));" in refresh
    assert "repaintTile(a.id, spec);" in refresh
    for other in ("loadApps", "loadHome", "location.reload"):
        assert other not in refresh, other
    tile = _js_function(page, "repaintTile")
    assert "home.tiles.find(x => x.app_id===appId)" in tile
    assert "t.spec = spec;" in tile
    # Drawn for its app, so an input on it acts on that app.
    assert "tile.replaceChildren(renderSpec(spec, t.name, appId));" in tile
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
    # A press on a picker or the gear is theirs, never a drag.
    assert "e.target.closest('select, .tile-gear')" in sortable
    assert "e.key==='ArrowLeft' || e.key==='ArrowUp'" in sortable


def test_home_reads_its_layout_and_writes_it_only_through_configure_or_placement(client):
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
    # Written in two places, both in Home's section, each its own function
    # and one save apiece - and nowhere else on the page.
    toggle = _js_function(page, "toggleConfigure")
    place = _js_function(page, "placeNewAppOnTab")
    assert "async function placeNewAppOnTab(appId, tabId){" in home
    assert page.count("api.saveLayout(") == home.count("api.saveLayout(") == 2
    assert toggle.count("api.saveLayout(") == place.count("api.saveLayout(") == 1
    # As Configure is switched off: the working copy, every tab, the one on
    # screen flushed into it first - its subtitle and its instances' order,
    # sizes, ids and configs.
    assert "const res = await api.saveLayout(home.layout);" in toggle
    assert toggle.index("flushTab();") < toggle.index("api.saveLayout(")
    # And as an install is put on the tab the Add popup picked: the layout
    # as the server reads it, never the working copy.
    assert "await api.saveLayout(layout);" in place
    assert place.index("api.homeLayout()") < place.index("api.saveLayout(")
    assert "layoutToSave" not in page
    assert _const(page, "layoutItems") == (
        "const layoutItems = () => home.tiles.map(t => ({instance_id:t.instance_id, app_id:t.app_id, "
        "size:{w:t.w, h:t.h}, config:t.config || {}}));"
    )
    flush = _js_function(page, "flushTab")
    assert "home.layout.tabs.find(t => t.id===home.activeTab)" in flush
    assert "tab.items = layoutItems();" in flush and "tab.subtitle = home.subtitle;" in flush


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
        _const(page, "GEAR"),
        _js_source(page, "configFields"),
        _js_source(page, "spanTile"),
        _js_source(page, "tileControls"),
        _js_source(page, "paintTiles"),
    ]) + """
        const renderSpec = (spec, name) => ({tag:'face', spec, name, className:'face'});
        let sortable = 0;
        const sortableTiles = grid => { sortable++; };
        // One tab: nowhere to move a tile to. Week declares a config field,
        // Steps none.
        const home = {activeTab:'home', layout:{default_tab:'home', tabs:[{id:'home', name:'Home', items:[]}]},
          specs:new Map([['week', {a:{config:[{key:'days', type:'number', default:7}]}}], ['steps', {a:{config:[]}}]])};
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
        const pick = grid.children[0].children[1].children[2];
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
    # Configure: every tile its handle; only the app with config fields a
    # gear, beside it; only the app with a choice of sizes a picker, its
    # sizes in the app's order, the current one selected.
    week, steps = result["configuring"]["tiles"]
    assert result["configuring"]["cls"] == "tiles configuring"
    assert week["parts"] == ["face", "tile-cfg"] and steps["parts"] == ["face", "tile-cfg"]
    assert week["controls"] == [
        {"cls": "tile-grip", "label": "Move Week <b>: drag it, or use the arrow keys", "options": []},
        {"cls": "tile-gear", "label": "Settings of Week <b>", "options": []},
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
        _js_source(page, "flushTab"),
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
        let reads = 0;
        const loadHome = () => { reads++; };
        const paintHomeHead = () => {}, paintHome = () => {}, paintTabStrip = () => {};
        const $ = () => ({setAttribute(){}});
        (async () => {
          const seen = [];
          const look = () => seen.push([home.configuring, reads]);
          await toggleConfigure(); look();
          home.tiles.reverse(); home.tiles[1].w = 3; home.subtitle = 'Edited';
          await toggleConfigure(); look();
          await toggleConfigure(); look();
          reply = {ok:false, error:'disk full'};
          await toggleConfigure(); look();
          console.log(JSON.stringify({seen, saved, notes, saving:home.saving}));
        })();
    """
    result = _node(script)

    # On, then off (saved, and Home read again as the server squares it),
    # on, then a failed save: still configuring, and nothing re-read.
    assert result["seen"] == [[True, 0], [False, 1], [True, 1], [True, 1]]
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


# ---------- multi-tab Home: the tab strip, switching, and managing tabs ----------
#
# The page's own functions, run under node over a small stand-in DOM -
# enough of one for the strip, the two panels and Home - against a layout
# the server read back (seeded with PUT), with every save the page sends
# put back to the server.


def _js(page, name):
    """A top-level function, const or let in the page's script, as written."""
    m = re.search(rf"\n  ((?:async )?function {re.escape(name)}\([\w, ]*\)\{{\n.*?\n  \}})\n", page, re.S)
    if m:
        return m.group(1)
    m = re.search(rf"\n  ((?:const|let) {re.escape(name)} = \{{\n.*?\n  \}};)\n", page, re.S)
    if m:
        return m.group(1)
    m = re.search(rf"\n  ((?:const|let) {re.escape(name)} = .*?;)(?: +//[^\n]*)?\n", page)
    assert m, name
    return m.group(1)


# A stand-in DOM: elements with children, classes, data-* and attributes,
# text, querySelector(All) for the selectors the page uses (compound
# simple selectors - tag, #id, .class, [attr], [attr="v"] - and
# descendants), remove(), and the listeners added (noted, with their
# options, for a test to call).
MINI_DOM = r"""
    const camel = s => s.replace(/-(\w)/g, (_, c) => c.toUpperCase());
    const attrOf = (n, k) => k.startsWith('data-') ? n.dataset[camel(k.slice(5))] : k==='id' ? n.id : n.attrs[k];
    const simple = (n, sel) => (sel.match(/[#.]?[\w-]+|\[[^\]]+\]/g) || []).every(p => {
      if(p[0]==='#') return n.id===p.slice(1);
      if(p[0]==='.') return (n.className || '').split(/\s+/).includes(p.slice(1));
      if(p[0]==='['){
        const [, k, v] = p.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
        const got = attrOf(n, k);
        return v===undefined ? got!==undefined && got!==null : String(got)===v;
      }
      return n.tag===p;
    });
    const matches = (n, sel) => {
      const chain = sel.trim().split(/\s+/);
      if(!simple(n, chain[chain.length-1])) return false;
      let i = chain.length-2, a = n.parent;
      for(; i >= 0 && a; a = a.parent) if(simple(a, chain[i])) i--;
      return i < 0;
    };
    const below = n => n.children.flatMap(c => [c, ...below(c)]);
    const mk = tag => {
      const n = {tag, children:[], style:{}, dataset:{}, attrs:{}, className:'', id:'', hidden:false, parent:null,
                 _text:'', _html:'', listeners:{}};
      n.setAttribute = (k, v) => { n.attrs[k] = String(v); if(k==='id') n.id = String(v); };
      n.getAttribute = k => k in n.attrs ? n.attrs[k] : null;
      n.removeAttribute = k => { delete n.attrs[k]; };
      n.appendChild = c => { c.parent = n; n.children.push(c); return c; };
      n.remove = () => { if(n.parent) n.parent.children = n.parent.children.filter(c => c!==n); n.parent = null; };
      n.addEventListener = (type, f, options) => { (n.listeners[type] = n.listeners[type] || []).push({f, options}); };
      n.replaceChildren = (...cs) => { cs.forEach(c => { c.parent = n; }); n.children = cs; n._html = ''; n._text = ''; };
      n.classList = {
        contains: c => n.className.split(/\s+/).includes(c),
        toggle: (c, on) => {
          const set = new Set(n.className.split(/\s+/).filter(Boolean));
          if(on===undefined ? !set.has(c) : on) set.add(c); else set.delete(c);
          n.className = [...set].join(' ');
        },
      };
      Object.defineProperty(n, 'textContent', {
        get: () => n.children.length ? n.children.map(c => c.textContent).join('') : n._text,
        set: v => { n.children = []; n._text = String(v); },
      });
      Object.defineProperty(n, 'innerHTML', {
        get: () => n._html,
        set: v => { n.children = []; n._text = ''; n._html = String(v); },
      });
      n.querySelectorAll = sel => sel.startsWith(':scope > ')
        ? n.children.filter(x => matches(x, sel.slice(9)))
        : below(n).filter(x => matches(x, sel));
      n.querySelector = sel => n.querySelectorAll(sel)[0] || null;
      n.focus = () => {};
      return n;
    };
    const document = mk('#document');
    document.createElement = mk;
"""

# The parts of the page the strip, the switch and Configure are made of.
TAB_PARTS = [
    "$", "el", "GRID_COLS", "SUBTITLE_MAX", "TAB_NAME_MAX", "home", "cells", "sizeOf", "appSizes",
    "loadHome", "renderActiveTab", "switchTab", "flushTab", "flowLayout", "layoutTiles", "layoutItems",
    "paintHome", "paintTiles", "spanTile", "paintHomeHead", "homeMsgTimer", "homeMsg", "paintTabStrip",
    "toggleConfigure", "GRIP", "tileControls", "moveTile", "createTab", "renameTab", "deleteTarget",
    "deleteTab", "newTabId", "tabNameProblem", "openTabEditor", "GEAR", "PANEL_TABS", "panelShown",
    "showTab", "showPanel", "markTabs", "OPENING_TAB", "tabButtons", "openingTab",
    "cfgByApp", "tileCfg", "configFields", "openTileConfig", "repaintTile", "timelines", "releaseTimelines",
]
# ... and the strip's own wiring, as the page has it.
TAB_WIRING = [
    "  document.querySelectorAll('.tab[data-tab]').forEach(b => { b.onclick = () => showTab(b.dataset.tab); });",
    "  $('#addTabBtn').onclick = () => { if(home.configuring && !home.saving) openTabEditor(null); };",
]

TAB_HARNESS = r"""
    // The page's markup, as far as these tests go: the strip - the stand-in
    // Home tab, Data, the hidden ＋ Add - and the two panels.
    const button = (cls, tab, text) => {
      const b = mk('button'); b.className = cls; if(tab) b.dataset.tab = tab;
      const label = mk('span'); label.textContent = text; b.appendChild(label);
      return b;
    };
    const nav = document.appendChild(mk('nav'));
    const region = nav.appendChild(mk('div'));
    region.id = 'layoutTabs'; region.className = 'layout-tabs';
    region.appendChild(button('tab', 'home', 'Home'));
    nav.appendChild(button('tab tab-end active', 'data', 'Data')).setAttribute('aria-current', 'page');
    const addBtn = nav.appendChild(button('tab tab-add', null, '＋ Add'));
    addBtn.id = 'addTabBtn'; addBtn.hidden = true;
    const homePanel = document.appendChild(mk('section'));
    homePanel.dataset.panel = 'home'; homePanel.hidden = true;
    ['homeHead', 'homeMsg', 'home'].forEach(id => { homePanel.appendChild(mk('div')).id = id; });
    document.appendChild(mk('section')).dataset.panel = 'data';

    // What the page draws with but these tests don't look at.
    const renderSpec = (spec, name) => { const n = mk('div'); n.className = 'face'; n.dataset.message = spec.message; return n; };
    const sortableTiles = () => {};
    const closeFloating = () => {};
    const setTimeout = () => 0, clearTimeout = () => {};
    const store = {};
    if(OPENING!==null) store['of-opening-tab'] = OPENING;
    const localStorage = {getItem: k => k in store ? store[k] : null, setItem: (k, v) => { store[k] = String(v); }};

    // The server, as the test left it: the apps, the layout (what a save
    // stored, from then on) and a spec each, saying what it was rendered
    // with - every render is noted, with the cfg it was sent.
    let served = LAYOUT;
    const saves = [], renders = [];
    const copy = x => JSON.parse(JSON.stringify(x));
    const api = {
      apps: async () => copy(APPS),
      homeLayout: async () => copy(served),
      renderApp: async (id, cfg) => {
        renders.push([id, cfg===undefined ? 'none' : copy(cfg)]);
        return {type:'empty', message:id+' '+JSON.stringify(cfg)};
      },
      saveLayout: async layout => { served = copy(layout); saves.push(copy(layout)); return {ok:true}; },
    };

    // The modals: each records its markup, and finds in it what the page
    // asks for - an #id, a [data-act]/[data-f] - and its [data-step]s.
    const modals = [];
    function openModal(html){
      const found = {};
      const steps = [...html.matchAll(/data-step="([\w-]+)"( hidden)?/g)].map(([, step, hidden]) => {
        const n = mk('div'); n.dataset.step = step; n.hidden = !!hidden; return n;
      });
      const box = {
        html,
        querySelector: sel => {
          if(!(sel in found)){
            const id = sel.match(/^#([\w-]+)$/), attr = sel.match(/^\[([\w-]+)="([^"]*)"\]$/);
            const there = id ? html.includes(`id="${id[1]}"`) : !!attr && html.includes(`${attr[1]}="${attr[2]}"`);
            found[sel] = there ? mk(sel) : null;
          }
          return found[sel];
        },
        querySelectorAll: sel => sel==='[data-step]' ? steps : [],
      };
      const modal = {box, closed:false, q: box.querySelector};
      modals.push(modal);
      return {box, close: () => { modal.closed = true; }};
    }

    WIRING

    // What's on screen.
    const tileOf = id => $('#home .tiles').children.find(t => t.dataset.app===id);
    const controlOf = (id, cls) => {
      const bar = tileOf(id).children.find(c => c.className==='tile-cfg');
      return bar ? bar.children.find(c => c.className===cls) || null : null;
    };
    const gearOf = id => { const kids = $('#layoutTabs').children; return kids[kids.findIndex(k => k.dataset.tab===id) + 1]; };
    const click = id => tabButtons().find(b => b.dataset.tab===id).onclick();
    const look = () => {
      const grid = $('#home .tiles'), head = $('#homeHead').children[0];
      return {
        strip: tabButtons().map(b => [b.dataset.tab, b.textContent, b.classList.contains('active'), b.getAttribute('aria-current')]),
        gears: $('#layoutTabs').children.filter(c => c.className==='tab-gear').map(g => g.attrs['aria-label']),
        add: !$('#addTabBtn').hidden,
        panels: document.querySelectorAll('[data-panel]').filter(p => !p.hidden).map(p => p.dataset.panel),
        tiles: grid ? grid.children.map(t => [t.dataset.app, t.style.gridColumn, t.style.gridRow]) : null,
        controls: grid ? grid.children.some(t => t.children.some(c => c.className==='tile-cfg')) : null,
        note: grid ? null : $('#home').innerHTML,
        subtitle: head ? (head.tag==='input' ? head.value : head.textContent) : null,
        active: home.activeTab,
      };
    };
    const tick = () => new Promise(r => setImmediate(r));
    // Boot, as the page does it: the shell painted, then Home read.
    async function boot(){
      showTab(openingTab());
      paintHomeHead();
      paintHome();
      const before = look();
      await loadHome();
      return before;
    }
    const r = {};
"""


def _tab_script(page, apps_listed, layout, scenario, opening=None):
    boot = page[page.index("BOOT - paint the shell") :]
    assert "\n  showTab(openingTab());\n" in boot and "\n  loadHome();\n" in boot
    for line in TAB_WIRING:
        assert line + "\n" in page, line
    harness = (
        TAB_HARNESS.replace("OPENING", json.dumps(opening))
        .replace("LAYOUT", json.dumps(layout))
        .replace("APPS", json.dumps(apps_listed))
        .replace("WIRING", "\n".join(TAB_WIRING))
    )
    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)
    return (
        MINI_DOM + esc + "\n" + "\n".join(_js(page, n) for n in TAB_PARTS) + harness
        + "(async () => {\n" + scenario + "\nconsole.log(JSON.stringify(r));\n})()"
        + ".catch(e => { console.error(e); process.exit(1); });"
    )


# a's config fields: a number with a label and a default, one with no
# type (so text), and a number with neither label nor default.
A_CONFIG = (
    "config:\n"
    '  - {key: goal, label: "Daily <goal>", type: number, default: 10000}\n'
    "  - {key: unit, label: Unit, default: steps}\n"
    "  - {key: cap, type: number}\n"
)


@pytest.fixture
def tab_apps(tmp_path, monkeypatch):
    """Four apps installed, in a temp apps folder: a with two sizes, c
    with two, b and d with one. Only a declares config fields."""
    import main

    path = tmp_path / "apps"
    for app_id, sizes in (
        ("a", "[{w: 1, h: 1}, {w: 2, h: 1}]"),
        ("b", "[{w: 1, h: 1}]"),
        ("c", "[{w: 2, h: 2}, {w: 1, h: 1}]"),
        ("d", "[{w: 1, h: 1}]"),
    ):
        (path / app_id).mkdir(parents=True)
        (path / app_id / "app.yaml").write_text(
            f"id: {app_id}\nname: App {app_id.upper()}\nsizes: {sizes}\n" + (A_CONFIG if app_id == "a" else "")
        )
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def _tab(tab_id, name, *items, subtitle=""):
    return {"id": tab_id, "name": name, "mode": "flow", "subtitle": subtitle, "items": list(items)}


# Two tabs, as a save would store them: a and b on Home, c on Two; d is
# placed nowhere, so the read puts it on the end of Home (the default).
SEEDED = {"tabs": [
    _tab("home", "Home", _instance("ia", "a", config={"goal": 5}), _instance("ib", "b"), subtitle="Today"),
    _tab("two", "Two <b>", _instance("ic", "c", 2, 2)),
], "default_tab": "home"}


def _seeded(client, layout=SEEDED):
    """The layout saved with PUT, then read back as the page reads it."""
    assert client.put("/api/home-layout", json=layout).status_code == 200
    return client.get("/api/apps").get_json(), client.get("/api/home-layout").get_json()


def _run_tabs(client, scenario, layout=SEEDED, opening=None):
    page = _page(client)
    listed, read = _seeded(client, layout)
    return _node(_tab_script(page, listed, read, scenario, opening)), read


def _stored(client, body):
    """A save the page sent, PUT to the server: what it then reads back."""
    resp = client.put("/api/home-layout", json=body)
    assert resp.status_code == 200, resp.get_json()
    return client.get("/api/home-layout").get_json()


def test_the_strip_holds_the_layouts_tabs_before_data(client):
    page = _page(client)
    # A region of the strip, filled from the layout, before Data; until
    # it's read, a stand-in for its first tab. ＋ Add starts hidden.
    assert re.search(
        r'<nav class="tabstrip" aria-label="Tabs">\s*(?:<!--.*?-->\s*)?'
        r'<div class="layout-tabs" id="layoutTabs">\s*'
        r'<button class="tab" data-tab="home" type="button"><span>Home</span></button>\s*</div>\s*'
        r'<button class="tab tab-end active" data-tab="data" type="button" aria-current="page"><span>Data</span></button>\s*'
        r'<button class="tab tab-add" id="addTabBtn" type="button" aria-haspopup="dialog" hidden><span>＋ Add</span></button>\s*'
        r"</nav>",
        page,
        re.S,
    )
    # The region lays its tabs out as the strip's own.
    assert re.search(r"^\.layout-tabs\{display:contents;\}", page, re.M)
    assert re.search(r"^\.tab\[hidden\]\{display:none;\}", page, re.M)
    assert _const(page, "TAB_NAME_MAX") == f"const TAB_NAME_MAX = {home_layout.MAX_TAB_NAME};"
    # A tab's name is only ever set as text.
    strip = _js_function(page, "paintTabStrip")
    assert "label.textContent = t.name;" in strip
    assert "${" not in strip and "innerHTML" not in strip
    # Data is the strip's own, never a layout tab; the rest share Home.
    assert _const(page, "PANEL_TABS") == "const PANEL_TABS = ['data'];"
    show = _js_function(page, "showTab")
    assert "if(PANEL_TABS.includes(name)) showPanel(name);" in show and "else switchTab(name);" in show


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_clicking_a_tab_draws_its_tiles_and_data_shows_the_console(client, tab_apps):
    r, read = _run_tabs(client, """
        r.loading = await boot();
        r.booted = look();
        click('two'); r.two = look();
        click('home'); r.home = look();
        click('data'); r.data = look();
        store['of-opening-tab'] = 'two'; r.opening = openingTab();
        r.saves = saves.length;
    """)
    # Booted on Data (no opening page chosen), the stand-in Home on the
    # strip; once read, the layout's tabs, by name, as text - and Home has
    # drawn the default tab underneath.
    assert r["loading"]["strip"] == [["home", "Home", False, None], ["data", "Data", True, "page"]]
    assert r["loading"]["panels"] == ["data"]
    data_on = [["home", "Home", False, None], ["two", "Two <b>", False, None], ["data", "Data", True, "page"]]
    assert r["booted"]["strip"] == data_on
    assert r["booted"]["panels"] == ["data"]
    assert [t[0] for t in r["booted"]["tiles"]] == ["a", "b", "d"]
    # Each tab draws its own tiles, into the one Home panel, and is the
    # strip's active tab.
    assert r["two"]["panels"] == ["home"]
    assert r["two"]["tiles"] == [["c", "span 2", "span 2"]]
    assert r["two"]["strip"] == [["home", "Home", False, None], ["two", "Two <b>", True, "page"], ["data", "Data", False, None]]
    assert r["two"]["subtitle"] == ""
    assert r["home"]["tiles"] == [["a", "span 1", "span 1"], ["b", "span 1", "span 1"], ["d", "span 1", "span 1"]]
    assert r["home"]["subtitle"] == "Today"
    assert r["data"]["panels"] == ["data"] and r["data"]["strip"] == data_on
    # Not configuring: just the tabs - no gears, no ＋ Add, no tile controls.
    for seen in (r["booted"], r["two"], r["home"]):
        assert seen["gears"] == [] and seen["add"] is False and seen["controls"] is False
    # The strip's buttons are what Settings' Opening page reads.
    assert r["opening"] == "two"
    # Viewing writes nothing.
    assert r["saves"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_add_makes_a_tab_with_a_minted_id_and_the_save_stores_it(client, tab_apps):
    r, read = _run_tabs(client, """
        await boot(); click('home');
        await toggleConfigure();
        r.configuring = look();
        $('#addTabBtn').onclick();
        const m = modals.pop();
        r.titled = m.box.html.includes('<h4>New tab</h4>');
        r.canDelete = m.q('[data-act="delete"]') !== null;
        m.q('#tabName').value = '  Training  ';
        m.q('[data-act="save"]').onclick();
        r.closed = m.closed;
        r.created = look();
        await toggleConfigure(); await tick();
        r.saves = saves;
        r.after = look();
    """)
    # Configure: every layout tab has its gear, and ＋ Add shows.
    assert r["configuring"]["gears"] == ["Tab settings: Home", "Tab settings: Two <b>"]
    assert r["configuring"]["add"] is True
    assert r["titled"] and r["canDelete"] is False and r["closed"]
    # A new, empty tab on the end of the strip, shown - its id minted on
    # the page, in the server's form.
    created = r["created"]
    new_id = created["active"]
    assert home_layout.TAB_ID.match(new_id) and new_id not in ("home", "two")
    assert created["strip"][:3] == [
        ["home", "Home", False, None], ["two", "Two <b>", False, None], [new_id, "Training", True, "page"],
    ]
    assert created["panels"] == ["home"] and created["tiles"] is None
    assert "Nothing on this tab yet" in created["note"]
    # Saved as Configure goes off: the new tab, as given; the server
    # takes its id and keeps it, empty.
    [body] = r["saves"]
    assert body["tabs"][2] == {"id": new_id, "name": "Training", "mode": "flow", "subtitle": "", "items": []}
    assert body["tabs"][:2] == read["tabs"]
    stored = _stored(client, body)
    assert stored == body
    assert r["after"]["add"] is False and r["after"]["gears"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_tab_is_renamed_from_its_gear_and_a_bad_name_is_refused_first(client, tab_apps):
    r, read = _run_tabs(client, """
        await boot(); click('two');
        await toggleConfigure();
        gearOf('two').onclick();
        const m = modals.pop();
        r.titled = m.box.html.includes('<h4>Tab settings</h4>');
        r.given = m.q('#tabName').value;
        r.tries = [];
        for(const name of ['   ', 'x'.repeat(41), 'Runs ☕ <i>']){
          m.q('#tabName').value = name; m.q('#tabName').oninput();
          m.q('[data-act="save"]').onclick();
          r.tries.push([m.q('[data-f="msg"]').textContent, m.closed, home.layout.tabs[1].name]);
        }
        r.renamed = look();
        await toggleConfigure();
        r.saves = saves;
    """)
    assert r["titled"]
    # The name in the box as text, as stored.
    assert r["given"] == "Two <b>"
    # Blank, then over the server's limit: refused in the popup, nothing
    # changed; then a good one, which closes it and relabels the tab.
    assert r["tries"] == [
        ["A tab needs a name.", False, "Two <b>"],
        [f"A tab's name can be at most {home_layout.MAX_TAB_NAME} characters.", False, "Two <b>"],
        ["", True, "Runs ☕ <i>"],
    ]
    assert r["renamed"]["strip"][1] == ["two", "Runs ☕ <i>", True, "page"]
    [body] = r["saves"]
    assert [t["name"] for t in body["tabs"]] == ["Home", "Runs ☕ <i>"]
    assert _stored(client, body) == body


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_pages_tab_name_check_is_the_servers(client):
    page = _page(client)
    names = ["Home", "", " ", "\t\n ", "a" * 40, "a" * 41, " " + "a" * 39, "☕" * 40, "☕" * 41,
             "🏃" * 40, "🏃" * 41, "<b>&</b>", " x ", 5, None]
    script = "\n".join([_const(page, "TAB_NAME_MAX"), _js_source(page, "tabNameProblem")]) + f"""
        console.log(JSON.stringify({json.dumps(names)}.map(n => tabNameProblem(n)===null)));
    """
    # Characters as Python counts them - code points, so 40 emoji pass.
    assert _node(script) == [home_layout._name_problem(n) is None for n in names]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_new_tabs_id_is_the_servers_form_and_never_one_taken(client):
    page = _page(client)
    script = _js_source(page, "newTabId") + """
        const ids = Array.from({length:200}, () => newTabId([]));
        // Random draws that come out as 'a' x 8, then 'b' x 8: the first is taken.
        const draws = [...Array(8).fill(10/36), ...Array(8).fill(11/36)];
        Math.random = () => draws.shift();
        console.log(JSON.stringify({ids, retaken: newTabId(['aaaaaaaa'])}));
    """
    result = _node(script)
    assert all(home_layout.TAB_ID.match(i) and len(i) == 8 for i in result["ids"]), result["ids"]
    assert "data" not in result["ids"]
    assert result["retaken"] == "bbbbbbbb"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_deleting_a_tab_moves_its_tiles_to_the_default(client, tab_apps):
    # The default isn't the first tab: deleted tiles go to the default,
    # not to whichever tab comes first.
    home, two = SEEDED["tabs"]
    layout = {"tabs": [two, _tab("three", "Three"), home], "default_tab": "home"}
    r, read = _run_tabs(client, """
        await boot(); click('two');
        await toggleConfigure();
        let m;
        gearOf('two').onclick(); m = modals.pop();
        m.q('[data-act="delete"]').onclick();
        r.steps = m.box.querySelectorAll('[data-step]').map(s => [s.dataset.step, s.hidden]);
        r.confirm = m.q('[data-f="confirm"]').textContent;
        m.q('[data-act="confirm"]').onclick();
        r.closed = m.closed;
        r.afterTwo = look();
        r.layoutAfterTwo = copy(home.layout);
        // The default, deleted from the Data tab: its tiles go to the first
        // tab left, which becomes the default - and Home draws it.
        click('data');
        gearOf('home').onclick(); m = modals.pop();
        m.q('[data-act="delete"]').onclick();
        r.confirmHome = m.q('[data-f="confirm"]').textContent;
        m.q('[data-act="confirm"]').onclick();
        r.afterHome = look();
        r.layoutAfterHome = copy(home.layout);
        // The only tab left: no Delete, and deleting it does nothing.
        gearOf('three').onclick(); m = modals.pop();
        r.lastCanDelete = m.q('[data-act="delete"]') !== null;
        deleteTab('three');
        r.left = home.layout.tabs.map(t => t.id);
        await toggleConfigure();
        r.saves = saves;
    """, layout=layout)
    home_items = read["tabs"][2]["items"]
    d = home_items[2]
    assert [i["app_id"] for i in home_items] == ["a", "b", "d"]
    # Asked first, saying where its tiles go - and that nothing's uninstalled.
    assert r["steps"] == [["name", True], ["delete", False]]
    assert r["confirm"] == "“Two <b>” goes, and its tile moves to “Home”. Nothing is uninstalled."
    assert r["closed"]
    # Two's instance on the end of Home, as it was - id, size, config -
    # and Home on screen in Two's place.
    assert r["layoutAfterTwo"]["tabs"] == [
        read["tabs"][1],
        {**read["tabs"][2], "items": home_items + [_instance("ic", "c", 2, 2)]},
    ]
    assert r["layoutAfterTwo"]["default_tab"] == "home"
    assert r["afterTwo"]["active"] == "home" and r["afterTwo"]["panels"] == ["home"]
    assert [t[0] for t in r["afterTwo"]["tiles"]] == ["a", "b", "d", "c"]
    assert [s[0] for s in r["afterTwo"]["strip"]] == ["three", "home", "data"]
    # Home itself: to Three, the new default, drawn though Data is on screen.
    assert r["confirmHome"] == "“Home” goes, and its 4 tiles move to “Three”. Nothing is uninstalled."
    assert r["layoutAfterHome"] == {"tabs": [
        {**read["tabs"][1], "items": home_items + [_instance("ic", "c", 2, 2)]},
    ], "default_tab": "three"}
    assert r["afterHome"]["panels"] == ["data"] and r["afterHome"]["active"] == "three"
    assert [t[0] for t in r["afterHome"]["tiles"]] == ["a", "b", "d", "c"]
    assert r["afterHome"]["strip"] == [["three", "Three", False, None], ["data", "Data", True, "page"]]
    assert r["lastCanDelete"] is False and r["left"] == ["three"]
    # Saved: one tab holding every instance, the ids as they were.
    [body] = r["saves"]
    assert body == r["layoutAfterHome"]
    stored = _stored(client, body)
    assert stored == body
    assert [i["instance_id"] for i in stored["tabs"][0]["items"]] == ["ia", "ib", d["instance_id"], "ic"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_tile_moved_to_another_tab_takes_its_instance_with_it(client, tab_apps):
    r, read = _run_tabs(client, """
        await boot(); click('home');
        await toggleConfigure();
        r.options = controlOf('a', 'tile-move').children.map(o => [o.value, o.textContent, !!o.disabled, !!o.selected]);
        // Resized first, then moved: it goes at that size.
        const size = controlOf('a', 'tile-size'); size.value = '1'; size.onchange();
        const move = controlOf('a', 'tile-move'); move.value = 'two'; move.onchange();
        r.moved = look();
        click('two'); r.onTwo = look();
        r.layout = copy(home.layout);
        await toggleConfigure();
        r.saves = saves;
    """)
    # The other tabs, by name, as text; this one isn't offered.
    assert r["options"] == [["", "Move…", True, True], ["two", "Two <b>", False, False]]
    # Off Home's grid, on the end of Two - its id, its size, its config.
    assert [t[0] for t in r["moved"]["tiles"]] == ["b", "d"]
    assert r["onTwo"]["tiles"] == [["c", "span 2", "span 2"], ["a", "span 2", "span 1"]]
    assert r["layout"]["tabs"][1]["items"] == [_instance("ic", "c", 2, 2), _instance("ia", "a", 2, 1, {"goal": 5})]
    assert [i["app_id"] for i in r["layout"]["tabs"][0]["items"]] == ["b", "d"]
    [body] = r["saves"]
    placed = [i["app_id"] for t in body["tabs"] for i in t["items"]]
    assert sorted(placed) == ["a", "b", "c", "d"]
    assert _stored(client, body) == body


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_tile_isnt_offered_a_tab_that_has_its_app_already(client, tab_apps):
    # A layout saved from elsewhere can place b on both tabs: moving
    # Home's b onto Two would merge them, so Two isn't offered for it.
    layout = {"tabs": [
        _tab("home", "Home", _instance("ia", "a"), _instance("ib", "b")),
        _tab("two", "Two", _instance("ic", "c", 2, 2), _instance("ib2", "b")),
        _tab("three", "Three"),
    ], "default_tab": "home"}
    r, _ = _run_tabs(client, """
        await boot(); click('home'); await toggleConfigure();
        const options = id => controlOf(id, 'tile-move').children.slice(1).map(o => o.value);
        r.a = options('a'); r.b = options('b');
    """, layout=layout)
    assert r["a"] == ["two", "three"]
    assert r["b"] == ["three"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_configures_edits_on_a_tab_survive_a_switch_away_and_back(client, tab_apps):
    r, read = _run_tabs(client, """
        await boot(); click('home');
        await toggleConfigure();
        // Reordered (as a drag leaves home.tiles), a resized, the subtitle
        // typed into its input.
        home.tiles.reverse();
        const size = controlOf('a', 'tile-size'); size.value = '1'; size.onchange();
        const input = $('#homeHead').children[0]; input.value = 'Edited'; input.oninput();
        click('two'); r.two = look();
        click('home'); r.back = look();
        await toggleConfigure();
        r.saves = saves;
    """)
    d = read["tabs"][0]["items"][2]
    assert r["two"]["tiles"] == [["c", "span 2", "span 2"]] and r["two"]["subtitle"] == ""
    # Back on Home, as it was left.
    assert r["back"]["tiles"] == [["d", "span 1", "span 1"], ["b", "span 1", "span 1"], ["a", "span 2", "span 1"]]
    assert r["back"]["subtitle"] == "Edited"
    [body] = r["saves"]
    assert body["tabs"][0] == _tab(
        "home", "Home", d, _instance("ib", "b"), _instance("ia", "a", 2, 1, {"goal": 5}), subtitle="Edited",
    )
    assert body["tabs"][1] == read["tabs"][1]
    assert _stored(client, body) == body


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
@pytest.mark.parametrize("opening, panel, shown, resolved", [
    # A layout tab: Home, straight away, then that tab once it's read.
    ("two", "home", "two", "two"),
    ("home", "home", "home", "home"),
    # One since deleted: Home's default tab, without an error.
    ("gone", "home", "home", "home"),
    # Data, or nothing chosen: the console.
    ("data", "data", "home", "data"),
    (None, "data", "home", "data"),
])
def test_the_page_opens_on_its_opening_tab_once_the_strip_has_it(client, tab_apps, opening, panel, shown, resolved):
    r, _ = _run_tabs(client, """
        r.before = await boot();
        r.after = look();
        r.opening = openingTab();
    """, opening=opening)
    assert r["before"]["panels"] == [panel] and r["after"]["panels"] == [panel]
    assert r["after"]["active"] == shown
    active = [s[0] for s in r["after"]["strip"] if s[2]]
    assert active == [shown if panel == "home" else "data"]
    assert r["opening"] == resolved


# ---------- a tile's settings: its instance's config ----------

# a's instance holds a setting and a key its form doesn't show; c's holds
# one though c declares no fields (a layout from elsewhere can). d is
# placed nowhere: the read puts it on Home, with config {}.
CONFIGURED = {"tabs": [
    _tab("home", "Home", _instance("ia", "a", config={"goal": 5, "old": "kept"}), _instance("ib", "b")),
    _tab("two", "Two", _instance("ic", "c", 2, 2, {"x": 1})),
], "default_tab": "home"}


def _message(app_id, cfg):
    """The face of a tile the harness drew: its spec's message, which names the cfg it was rendered with."""
    return app_id + " " + json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_home_renders_each_app_with_its_instances_config_and_a_gear_only_for_fields(client, tab_apps):
    r, read = _run_tabs(client, """
        await boot(); click('home');
        r.boot = copy(renders);
        r.faces = $('#home .tiles').children.map(t => [t.dataset.app, t.children[0].dataset.message]);
        r.cfgs = ['a', 'b', 'c', 'd', 'nope'].map(tileCfg);
        await toggleConfigure();
        const gear = id => { const g = controlOf(id, 'tile-gear'); return g && [g.attrs['aria-label'], g.attrs['aria-haspopup']]; };
        r.gears = ['a', 'b', 'd'].map(gear);
        r.bar = tileOf('a').children[1].children.map(c => c.className);
        click('two'); r.gearsTwo = [gear('c')];
    """, layout=CONFIGURED)
    # One render an app, in folder order, each sent its instance's config.
    assert r["boot"] == [["a", {"goal": 5, "old": "kept"}], ["b", {}], ["c", {"x": 1}], ["d", {}]]
    assert r["faces"] == [
        ["a", _message("a", {"goal": 5, "old": "kept"})], ["b", _message("b", {})], ["d", _message("d", {})],
    ]
    # What Refresh renders with: the tile's on screen, else the layout's.
    assert r["cfgs"] == [{"goal": 5, "old": "kept"}, {}, {"x": 1}, {}, {}]
    # A gear only where the app declares config fields - not for c, whose
    # instance holds a config its app has no form for - beside the handle.
    assert r["gears"] == [["Settings of App A", "dialog"], None, None]
    assert r["gearsTwo"] == [None]
    assert r["bar"] == ["tile-grip", "tile-gear", "tile-size", "tile-move"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_tiles_settings_save_into_its_instance_rerender_it_and_go_with_the_layout(client, tab_apps):
    r, read = _run_tabs(client, """
        await boot(); click('home');
        await toggleConfigure();
        const before = renders.length;
        controlOf('a', 'tile-gear').onclick();
        const m = modals.pop();
        r.html = m.box.html;
        r.inputs = [0, 1, 2, 3].map(i => m.q('#tileCfg'+i) !== null);
        r.given = [0, 1, 2].map(i => m.q('#tileCfg'+i).value);
        m.q('#tileCfg0').value = '12500';
        m.q('#tileCfg1').value = 'km <i>';
        m.q('#tileCfg2').value = '3';
        m.q('[data-act="save"]').onclick();
        r.closed = m.closed;
        r.config = copy(home.tiles.find(t => t.app_id==='a').config);
        await tick();
        r.rendered = renders.slice(before);
        r.tile = tileOf('a').children.map(c => [c.className, c.dataset.message || null]);
        // A refresh mid-Configure (a sync, an install) renders with the
        // setting not saved yet, and keeps it.
        const mid = renders.length;
        await loadHome();
        r.refreshed = renders.slice(mid);
        r.kept = copy(home.tiles.find(t => t.app_id==='a').config);
        r.refreshCfg = tileCfg('a');
        await toggleConfigure(); await tick();
        r.saves = saves;
        r.reread = renders.slice(-4);
    """, layout=CONFIGURED)
    html = r["html"]
    # Titled by the app's name; a field per schema entry, in order -
    # labelled by its label, else its key, escaped; a number field a
    # numeric input, the rest text - and no values in the markup.
    assert "<h4>App A settings</h4>" in html
    assert '<label for="tileCfg0">Daily &lt;goal&gt;</label>' in html
    assert 'id="tileCfg0" type="number" step="any"' in html
    assert '<label for="tileCfg1">Unit</label>' in html and 'id="tileCfg1" type="text"' in html
    assert '<label for="tileCfg2">cap</label>' in html and 'id="tileCfg2" type="number" step="any"' in html
    assert "value=" not in html and "kept" not in html
    assert r["inputs"] == [True, True, True, False]
    # Filled from the instance's config, else the field's default, else blank.
    assert r["given"] == ["5", "steps", ""]
    # Saved into the instance - the number a number, text as typed, the
    # key the form doesn't show kept - and the tile rendered once with it,
    # its face redrawn in place, its controls kept.
    config = {"goal": 12500, "old": "kept", "unit": "km <i>", "cap": 3}
    assert r["closed"] and r["config"] == config
    assert r["rendered"] == [["a", config]]
    assert r["tile"] == [["face", _message("a", config)], ["tile-cfg", None]]
    assert ["a", config] in r["refreshed"] and r["kept"] == config and r["refreshCfg"] == config
    # Configure off: the instance's config goes with the layout, and the
    # re-read renders with what was stored.
    [body] = r["saves"]
    assert body["tabs"][0]["items"][0] == _instance("ia", "a", config=config)
    assert body["tabs"][1] == read["tabs"][1]
    stored = _stored(client, body)
    assert stored == body
    assert r["reread"] == [["a", config], ["b", {}], ["c", {"x": 1}], ["d", {}]]

    # Reloaded: the form opens on the saved values.
    r, _ = _run_tabs(client, """
        await boot(); click('home');
        r.boot = renders.filter(x => x[0]==='a');
        await toggleConfigure();
        controlOf('a', 'tile-gear').onclick();
        const m = modals.pop();
        r.given = [0, 1, 2].map(i => m.q('#tileCfg'+i).value);
    """, layout=stored)
    assert r["boot"] == [["a", config]]
    assert r["given"] == ["12500", "km <i>", "3"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_number_setting_saves_a_number_or_falls_back_to_its_default(client, tab_apps):
    r, _ = _run_tabs(client, """
        await boot(); click('home'); await toggleConfigure();
        const cfgA = () => copy(home.tiles.find(t => t.app_id==='a').config);
        const set = async (goal, cap, act) => {
          controlOf('a', 'tile-gear').onclick();
          const m = modals.pop();
          m.q('#tileCfg0').value = goal; m.q('#tileCfg2').value = cap;
          m.q('[data-act="'+(act || 'save')+'"]').onclick();
          await tick();
          return cfgA();
        };
        r.tries = [];
        for(const [goal, cap] of [['8000.5', '0'], ['  ', ''], ['abc', 'Infinity'], ['1e3', ' 2 '], ['-4', '']]){
          r.tries.push(await set(goal, cap));
        }
        // Cancel changes nothing and renders nothing.
        const n = renders.length;
        r.cancelled = await set('1', '1', 'close');
        r.cancelRenders = renders.length - n;
        // Enter in a field saves, as the button does.
        controlOf('a', 'tile-gear').onclick();
        const m = modals.pop();
        m.q('#tileCfg0').value = '42';
        m.q('#tileCfg1').onkeydown({key:'Enter'});
        r.entered = [m.closed, cfgA()];
        // A render that fails is an error tile, the setting kept.
        api.renderApp = async () => { throw new Error('offline'); };
        await set('7', '');
        r.failed = [tileOf('a').children[0].dataset.message, cfgA().goal];
    """, layout=CONFIGURED)
    kept = {"old": "kept", "unit": "steps"}
    assert r["tries"] == [
        {**kept, "goal": 8000.5, "cap": 0},
        # Blank: the default - and with no default, no key at all.
        {**kept, "goal": 10000},
        # Not a number, or not a finite one: the same.
        {**kept, "goal": 10000},
        {**kept, "goal": 1000, "cap": 2},
        {**kept, "goal": -4},
    ]
    assert r["cancelled"] == {**kept, "goal": -4} and r["cancelRenders"] == 0
    assert r["entered"] == [True, {**kept, "goal": 42}]
    assert r["failed"] == ["Couldn't load this app.", 7]


def test_the_tile_settings_form_sets_values_as_text_never_markup(client):
    page = _page(client)
    form = _js_function(page, "openTileConfig")
    # Labels and the name escaped into the markup; values only ever .value.
    assert "${esc(t.name || t.app_id)} settings" in form
    assert "${esc(f.label || f.key)}" in form
    assert "inputs[i].value = v==null ? '' : String(v);" in form
    assert "${v" not in form and "${cfg" not in form and "${f.default" not in form
    # Saved into the instance, re-rendered with it: no route of its own.
    assert "t.config = next;" in form
    assert "api.renderApp(t.app_id, next)" in form
    assert "repaintTile(t.app_id, spec)" in form
    assert "saveLayout" not in form
    assert "api.renderApp(a.id, cfgs.get(a.id) || {})" in _js_function(page, "loadHome")
    # The render route is a POST carrying the cfg, through the API block.
    assert "renderApp:    (id, cfg)      => postJSON('/api/app/'+enc(id)+'/render', {cfg: cfg || {}})," in page


# ---------- multi-tab Home, slice 4: Duplicate an app ----------
#
# Duplicate, in an open app row, forks the app: its files read back from
# the file route, its app.yaml given the next free id and name, and the
# Add popup opened on them to review and install - two apps, not two
# tiles. The page's own appPanel, openAddApp and fileSlot run under node
# over the stand-in DOM, taught to parse the page's markup, against the
# files and listing the server hands out; what the popup installs is then
# POSTed to the server.

# MINI_DOM, plus what the popup needs: innerHTML parsed into elements -
# tags, attributes and text, entities decoded (the page's markup is
# well-formed, its attributes double-quoted), a field's value '' as a
# browser starts it - classList.add and remove().
_SETTER = "n._html = String(v); },"
_CLASSES = "      n.classList = {\n"
assert MINI_DOM.count(_SETTER) == 1 and MINI_DOM.count(_CLASSES) == 1
PARSING_DOM = MINI_DOM.replace(
    _SETTER, "n._html = String(v); parseInto(n, n._html); },"
).replace(
    _CLASSES,
    "      n.remove = () => { if(n.parent){ n.parent.children.splice(n.parent.children.indexOf(n), 1); n.parent = null; } };\n"
    + _CLASSES + "        add: c => n.classList.toggle(c, true),\n",
) + r"""
    const VOID = new Set(['input', 'br', 'img', 'hr']);
    const unent = s => s.replace(/&(amp|lt|gt|quot|#39|#10);/g, (_, e) =>
      ({amp:'&', lt:'<', gt:'>', quot:'"', '#39':"'", '#10':'\n'})[e]);
    function parseInto(root, html){
      const stack = [root];
      const tokens = /<!--[\s\S]*?-->|<\/([\w-]+)\s*>|<([\w-]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>|([^<]+)/g;
      for(const [, close, open, attrs, text] of html.matchAll(tokens)){
        const top = stack[stack.length-1];
        if(close){
          while(stack.length > 1 && stack.pop().tag !== close);
        }else if(open){
          const n = top.appendChild(mk(open));
          for(const [, k, v] of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)){
            const val = v===undefined ? '' : unent(v);
            if(k==='class') n.className = val;
            else if(k==='id') n.id = val;
            else if(k==='hidden') n.hidden = true;
            else if(k.startsWith('data-')) n.dataset[camel(k.slice(5))] = val;
            else n.attrs[k] = val;
          }
          if(open==='textarea' || open==='input') n.value = '';
          if(!VOID.has(open)) stack.push(n);
        }else if(text && text.trim()){
          top.appendChild(mk('#text'))._text = unent(text);
        }
      }
    }
    document.body = document.appendChild(mk('body'));
    document.addEventListener = document.removeEventListener = () => {};
"""

# The parts of the page a duplicate goes through: the row, the popup (and
# Home's state, whose layout its Review reads for the tab picker - not read
# here, so no picker).
DUPE_PARTS = [
    "$", "el", "home", "appsList", "appPanelId", "appPanel", "writesTicker", "openModal",
    "INSTALL_ID", "CUSTOM_CODE", "fileNameProblem", "NO_ID", "yamlId", "manifestId", "updateId",
    "dupeTarget", "bumpManifest", "fileSlot", "slotFiles", "openAddApp",
]

DUPE_HARNESS = r"""
    const metricName = k => k;
    const setTimeout = () => 0, clearTimeout = () => {};
    const copy = x => JSON.parse(JSON.stringify(x));
    // The server, as the test left it: the apps it lists, and each file's
    // reply from the file route, as send() hands it on. Every read and
    // install is noted; an install is answered as the server would.
    const APPS_ = APPS, FILES_ = FILES;
    const reads = [], installs = [], installed = [], updates = [];
    let updateReply = null;   // the server's answer to an update: null, a success
    const api = {
      appFile: async (id, name) => { reads.push([id, name]); return copy(FILES_[id+'/'+name]); },
      installApp: async files => {
        installs.push(copy(files));
        return {ok:true, id:manifestId(files['app.yaml']).id};
      },
      updateApp: async (id, files) => {
        updates.push([id, copy(files)]);
        return updateReply || {ok:true, id, has_app_py:'app.py' in files};
      },
    };
    const appInstalled = (res, done) => { installed.push([res, done]); };
    appsList.apps = copy(APPS_);
    const r = {};

    // An app's open row, and its Duplicate clicked: what it read, what it
    // said, and the popup it opened - its slots, then Review and Install.
    async function duplicate(id){
      const row = appPanel(APPS_.find(a => a.id===id));
      const out = {acts: row.querySelectorAll('.a-acts button').map(b => [b.dataset.act, b.textContent, b.className])};
      const dupe = row.querySelector('[data-act="duplicate"]');
      const before = document.body.children.length;
      const pending = dupe.onclick();
      out.busy = dupe.disabled;
      await pending;
      out.after = dupe.disabled;
      out.reads = reads.splice(0);
      out.msg = row.querySelector('.a-msg').textContent;
      if(document.body.children.length===before){ out.popup = null; return out; }
      const bg = document.body.children[document.body.children.length-1], box = bg.children[0];
      const q = s => box.querySelector(s);
      out.popup = {
        title: q('h4').textContent,
        hint: q('[data-step="files"] .hint').textContent.replace(/\s+/g, ' '),
        slots: box.querySelectorAll('.fslot').map(s =>
          [s.dataset.file || '', s.querySelector('.fname') ? s.querySelector('.fname').value : null,
           s.querySelector('textarea').value]),
      };
      q('[data-act="review"]').onclick();
      out.review = {
        id: q('[data-f="id"]').textContent, problem: q('[data-f="msg"]').textContent,
        files: box.querySelectorAll('[data-f="files"] li').map(li => li.textContent),
        notice: !q('[data-f="notice"]').hidden, install: !q('[data-act="install"]').disabled,
      };
      await q('[data-act="install"]').onclick();
      out.installs = installs.splice(0);
      out.installed = installed.splice(0);
      out.closed = !bg.parent;
      return out;
    }
"""


def _dupe_script(page, listed, files, scenario):
    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)
    harness = DUPE_HARNESS.replace("APPS_ = APPS", f"APPS_ = {json.dumps(listed)}").replace(
        "FILES_ = FILES", f"FILES_ = {json.dumps(files)}"
    )
    return (
        PARSING_DOM + esc + "\n" + "\n".join(_js(page, n) for n in DUPE_PARTS) + harness
        + "(async () => {\n" + scenario + "\nconsole.log(JSON.stringify(r));\n})()"
        + ".catch(e => { console.error(e); process.exit(1); });"
    )


def _served(client):
    """What the page would read: /api/apps, and every file it lists from
    the file route, each reply as send() hands it on - {ok, ...body}."""
    listed = client.get("/api/apps").get_json()
    files = {}
    for a in listed:
        for name in a["files"]:
            resp = client.get(f"/api/app/{a['id']}/file", query_string={"name": name})
            files[f"{a['id']}/{name}"] = {"ok": 200 <= resp.status_code < 300, **resp.get_json()}
    return listed, files


def _run_dupe(client, scenario):
    listed, files = _served(client)
    return _node(_dupe_script(_page(client), listed, files, scenario))


@pytest.fixture
def dupe_apps(tmp_path, monkeypatch):
    """The two real apps, steps with two more files - one empty - and pics,
    whose logo isn't text."""
    import main

    path = tmp_path / "apps"
    fixtures = os.path.join(os.path.dirname(__file__), "fixtures", "apps")
    for app_id in ("steps", "steps-metres"):
        shutil.copytree(os.path.join(fixtures, app_id), path / app_id,
                        ignore=shutil.ignore_patterns("__pycache__"))
    (path / "steps" / "notes.txt").write_text("stride: 0.75 m\n")
    (path / "steps" / "empty.txt").write_text("")
    (path / "pics").mkdir()
    (path / "pics" / "app.yaml").write_text("id: pics\nname: Pics\nsizes: [{w: 1, h: 1}]\n")
    (path / "pics" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def test_an_open_app_row_has_duplicate_beside_uninstall(client):
    page = _page(client)
    panel = _js_function(page, "appPanel")
    # A plain button, before Uninstall at the row's far end - after Update -
    # opening a popup.
    assert re.search(
        r'<span class="a-acts-end">\s*'
        r'<button class="btn sm" data-act="update" type="button" aria-haspopup="dialog">Update</button>\s*'
        r'<button class="btn sm" data-act="duplicate" type="button" aria-haspopup="dialog">Duplicate</button>\s*'
        r'<button class="btn sm danger" data-act="uninstall" type="button">Uninstall</button>\s*'
        r"</span>",
        panel,
    )
    # It reads every file through the file route, as Update does, and
    # opens the Add popup on the copy - it installs nothing itself.
    read = panel[panel.index("const readBack = async (btn, verb) => {") : panel.index("const upd = act('update');")]
    assert "await Promise.all(files.map(f => api.appFile(a.id, f)))" in read
    handler = panel[panel.index("dupe.onclick = async () => {") : panel.index("const gateOpen")]
    assert "const copy = await readBack(dupe, 'duplicate');" in handler
    assert "dupeTarget(a, appsList.apps || [])" in handler
    assert "bumpManifest(manifest.text, to.id, to.name)" in handler
    assert "openAddApp(copy, name);" in handler
    for write in ("installApp", "updateApp", "uninstallApp", "loadApps", "loadHome"):
        assert write not in handler, write
    # The copy's names and texts go into the slots as values, never markup.
    slot = _js_function(page, "fileSlot")
    assert "text.value = prefill.text;" in slot and "if(name) name.value = prefill.name;" in slot
    assert "${prefill" not in slot
    assert "${esc(copyOf)}" in _js_function(page, "openAddApp")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_duplicate_opens_the_add_popup_on_a_copy_that_installs_as_a_new_app(client, dupe_apps):
    r = _run_dupe(client, "r.steps = await duplicate('steps');")
    out = r["steps"]
    # The row's actions: Refresh, then Update and Duplicate beside Uninstall.
    assert out["acts"] == [
        ["refresh", "Refresh", "btn sm"],
        ["update", "Update", "btn sm"],
        ["duplicate", "Duplicate", "btn sm"],
        ["uninstall", "Uninstall", "btn sm danger"],
    ]
    # Every file read, the button off while they're read.
    assert out["reads"] == [["steps", f] for f in ("app.py", "app.yaml", "empty.txt", "notes.txt")]
    assert (out["busy"], out["after"], out["msg"]) == (True, False, "")

    # The popup, prefilled: app.yaml with the next free id and name, the
    # rest of it as written; app.py in its slot; an added slot for each
    # other file, named, the empty one kept.
    original = {p.name: p.read_text() for p in (dupe_apps / "steps").iterdir() if p.is_file()}
    manifest = original["app.yaml"].replace("id: steps\nname: Steps\n", 'id: steps-2\nname: "Steps 2"\n')
    assert manifest != original["app.yaml"]
    popup = out["popup"]
    assert popup["title"] == "Duplicate application"
    assert popup["hint"].startswith("A copy of Steps: its files, with a new id and name in its app.yaml.")
    assert popup["slots"] == [
        ["app.yaml", None, manifest],
        ["app.py", None, original["app.py"]],
        ["", "empty.txt", ""],
        ["", "notes.txt", "stride: 0.75 m\n"],
    ]
    # Review: the new id, every file, and - there's an app.py - the
    # custom-code notice, to consent to the copied code.
    assert out["review"] == {
        "id": "Install application: steps-2", "problem": "",
        "files": ["app.yaml", "app.py", "empty.txt", "notes.txt"], "notice": True, "install": True,
    }
    # Install: the copy, sent as the popup installs any app.
    [body] = out["installs"]
    assert body == {**original, "app.yaml": manifest}
    assert out["installed"] == [[{"ok": True, "id": "steps-2"}, "Installed"]] and out["closed"] is True

    # ... which the server installs as a second app, beside the first.
    resp = client.post("/api/apps", json={"files": body})
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["id"] == "steps-2"
    assert {p.name: p.read_text() for p in (dupe_apps / "steps-2").iterdir() if p.is_file()} == body
    listed = client.get("/api/apps").get_json()
    assert [(a["id"], a["name"]) for a in listed] == [
        ("pics", "Pics"), ("steps", "Steps"), ("steps-2", "Steps 2"), ("steps-metres", "Distance (from steps)"),
    ]
    # Home places it: its own instance, at the end of the default tab.
    layout = client.get("/api/home-layout").get_json()
    [tab] = layout["tabs"]
    assert [i["app_id"] for i in tab["items"]] == ["pics", "steps", "steps-2", "steps-metres"]
    assert len({i["instance_id"] for i in tab["items"]}) == 4
    # ... rendering as the original does.
    first = client.post("/api/app/steps/render", json={"cfg": {}}).get_json()
    assert client.post("/api/app/steps-2/render", json={"cfg": {}}).get_json() == first
    assert first["type"] != "error"

    # Duplicated again, from the original or the copy: the next in line.
    r = _run_dupe(client, "r.steps = await duplicate('steps'); r.copy = await duplicate('steps-2');")
    for out in (r["steps"], r["copy"]):
        assert out["popup"]["slots"][0][2].startswith('id: steps-3\nname: "Steps 3"\n')
        assert out["review"]["id"] == "Install application: steps-3"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_duplicate_has_its_own_settings(client, conn, dupe_apps):
    r = _run_dupe(client, "r.out = await duplicate('steps-metres');")
    out = r["out"]
    assert out["popup"]["slots"][0][2].startswith('id: steps-metres-2\nname: "Distance (from steps) 2"\n')
    [body] = out["installs"]
    assert client.post("/api/apps", json={"files": body}).status_code == 201

    # Two apps, two instances, each its own stride - saved, read back, and
    # rendered with it.
    layout = client.get("/api/home-layout").get_json()
    items = {i["app_id"]: i for i in layout["tabs"][0]["items"]}
    items["steps-metres"]["config"] = {"stride_m": 0.75}
    items["steps-metres-2"]["config"] = {"stride_m": 1.5}
    stored = _stored(client, layout)
    configs = {i["app_id"]: i["config"] for i in stored["tabs"][0]["items"]}
    assert configs["steps-metres"] == {"stride_m": 0.75} and configs["steps-metres-2"] == {"stride_m": 1.5}
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit) VALUES (?, 'garmin', 'steps', 4000, 'count')",
        (date.today().isoformat(),),
    )
    recompute_derived(conn)
    conn.commit()
    values = [
        client.post(f"/api/app/{app_id}/render", json={"cfg": configs[app_id]}).get_json()["value"]
        for app_id in ("steps-metres", "steps-metres-2")
    ]
    assert values == [3000, 6000]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_file_that_cant_be_read_stops_the_duplicate(client, dupe_apps):
    r = _run_dupe(client, """
        r.pics = await duplicate('pics');
        // A read that fails on the way - the network - stops it too.
        FILES_['steps/app.py'] = {ok:false, error:'network error'};
        r.steps = await duplicate('steps');
    """)
    # The server's refusal, named in the row; no popup, nothing installed,
    # the button back on.
    pics = r["pics"]
    assert pics["reads"] == [["pics", "app.yaml"], ["pics", "logo.png"]]
    assert pics["msg"] == "Couldn't read logo.png ('logo.png' isn't text, so it can't be shown) — can't duplicate Pics."
    assert (pics["popup"], pics["busy"], pics["after"]) == (None, True, False)
    assert r["steps"]["msg"] == "Couldn't read app.py (network error) — can't duplicate Steps."
    assert r["steps"]["popup"] is None
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["pics", "steps", "steps-metres"]


# (the app, the apps installed) -> its duplicate's id and name.
DUPE_TARGETS = [
    ({"id": "steps", "name": "Steps"}, [("steps", "Steps")], ("steps-2", "Steps 2")),
    ({"id": "steps", "name": "Steps"}, [("steps", "Steps"), ("steps-2", "Steps 2")], ("steps-3", "Steps 3")),
    # A copy's copy counts on from the same base.
    ({"id": "steps-2", "name": "Steps 2"}, [("steps", "Steps"), ("steps-2", "Steps 2")], ("steps-3", "Steps 3")),
    # The id and the name move in step: either taken, the next n.
    ({"id": "steps", "name": "Steps"}, [("steps", "Steps"), ("steps-2", "Other")], ("steps-3", "Steps 3")),
    ({"id": "steps", "name": "Steps"}, [("steps", "Steps"), ("other", "Steps 2")], ("steps-3", "Steps 3")),
    # The lowest free.
    ({"id": "steps", "name": "Steps"}, [("steps", "Steps"), ("steps-3", "Steps 3")], ("steps-2", "Steps 2")),
    # The name defaults to the id.
    ({"id": "steps", "name": "steps"}, [("steps", "steps")], ("steps-2", "steps 2")),
    ({"id": "steps"}, [("steps", "steps")], ("steps-2", "steps 2")),
    # A hand-placed app's id, put in the installer's form.
    ({"id": "My_App", "name": "My App"}, [("My_App", "My App")], ("my-app-2", "My App 2")),
    ({"id": "run-10", "name": "Run10"}, [("run-10", "Run10")], ("run-2", "Run10 2")),
    ({"id": "2024", "name": "2"}, [("2024", "2")], ("2024-2", "2 2")),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_duplicates_are_numbered_in_step_from_two(client):
    page = _page(client)
    cases = [[app, [{"id": i, "name": n} for i, n in listed]] for app, listed, _ in DUPE_TARGETS]
    script = _js(page, "dupeTarget") + f"""
        console.log(JSON.stringify({json.dumps(cases)}.map(([app, listed]) => dupeTarget(app, listed))));
    """
    result = _node(script)
    assert [(t["id"], t["name"]) for t in result] == [want for _, _, want in DUPE_TARGETS]
    # Every id one an install takes.
    for target in result:
        assert apps.INSTALL_ID.match(target["id"]), target


# (an app.yaml, the new id and name) -> the app.yaml bumped: the id and
# name lines replaced - the last top-level one of each, as manifestId and
# YAML read them - and every other line as written.
BUMPS = [
    ("id: steps\nname: Steps\nsizes: [{ w: 1, h: 1 }]\nreads: [steps]\n", "steps-2", "Steps 2",
     'id: steps-2\nname: "Steps 2"\nsizes: [{ w: 1, h: 1 }]\nreads: [steps]\n'),
    # No name: one goes in under the id.
    ("id: steps\nsizes: [{ w: 1, h: 1 }]\n", "steps-2", "steps 2",
     'id: steps-2\nname: "steps 2"\nsizes: [{ w: 1, h: 1 }]\n'),
    # Name first; comments and nested ids and names left alone.
    ("# My app\nname: Steps # shown\nconfig:\n  - { key: id, label: name }\nsettings:\n  id: nested\n"
     "  name: nested\nid: 'steps'\nsizes: [{w: 1, h: 1}]\n", "steps-2", "Steps 2",
     '# My app\nname: "Steps 2"\nconfig:\n  - { key: id, label: name }\nsettings:\n  id: nested\n'
     "  name: nested\nid: steps-2\nsizes: [{w: 1, h: 1}]\n"),
    # The last of a repeated key, as YAML keeps.
    ("id: first\nid: steps\nname: A\nname: B\nsizes: [{w: 1, h: 1}]", "steps-2", "B 2",
     'id: first\nid: steps-2\nname: A\nname: "B 2"\nsizes: [{w: 1, h: 1}]'),
    # CRLF kept.
    ("id: steps\r\nname: Steps\r\nsizes: [{w: 1, h: 1}]\r\n", "steps-2", "Steps 2",
     'id: steps-2\r\nname: "Steps 2"\r\nsizes: [{w: 1, h: 1}]\r\n'),
    # No id (it was the folder's name): one goes in above the first key,
    # after any comment or document marker.
    ("# c\n---\nname: Steps\nsizes: [{w: 1, h: 1}]\n", "steps-2", "Steps 2",
     '# c\n---\nid: steps-2\nname: "Steps 2"\nsizes: [{w: 1, h: 1}]\n'),
    ("sizes: [{w: 1, h: 1}]\n", "x-2", "x 2", 'id: x-2\nname: "x 2"\nsizes: [{w: 1, h: 1}]\n'),
]

# Names a duplicate can carry - anything an app's name can be - each one
# YAML string once bumped.
TRICKY_NAMES = [
    'He said "hi" 2', "back\\slash\\ 2", "it's 2", "# not a comment 2", "key: value 2", "- dash 2",
    "[list] {map} 2", "&anchor *alias !tag %pct @at `tick` | > 2", "  padded  2", "ünï 名前 😀 2",
    "tab\there 2", "line\nbreak 2", "sep par nel\x85 2", "nul\x00del\x7f 2", "\\u0041 2", "\\\" 2",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_bump_manifest_rewrites_the_id_and_name_and_keeps_the_rest(client):
    page = _page(client)
    yamls = [[text, new_id, name] for text, new_id, name, _ in BUMPS]
    tricky = [["id: steps\nname: Steps\nsizes: [{w: 1, h: 1}]\n", "steps-2", n] for n in TRICKY_NAMES]
    script = "\n".join([
        _js(page, "INSTALL_ID"), _js(page, "NO_ID"), _js(page, "yamlId"), _js(page, "manifestId"),
        _js(page, "bumpManifest"),
    ]) + f"""
        const bump = cases => cases.map(([t, id, name]) => bumpManifest(t, id, name));
        const bumped = bump({json.dumps(yamls)}), tricky = bump({json.dumps(tricky)});
        console.log(JSON.stringify({{bumped, tricky, previews: bumped.concat(tricky).map(manifestId)}}));
    """
    result = _node(script)

    assert result["bumped"] == [want for *_, want in BUMPS]
    for (text, new_id, name, _), out in zip(BUMPS + [(*t, None) for t in tricky], result["bumped"] + result["tricky"]):
        # YAML reads the new id and name, and the server takes the id ...
        manifest = yaml.safe_load(out)
        assert (manifest["id"], manifest["name"]) == (new_id, name), out
        assert apps._install_id(apps._manifest_to_install({"app.yaml": out})) == new_id
        # ... as the popup's Review preview does.
        assert apps.manifest_sizes(manifest)
    assert result["previews"] == [{"id": t[1]} for t in yamls + tricky]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_duplicate_popup_sets_names_and_texts_as_values_never_markup(client):
    page = _page(client)
    script = _dupe_script(page, [], {}, """
        openAddApp([{name:'app.yaml', text:'id: x <b>'}, {name:'<i>.txt', text:'<b>bold</b> &amp;'}], 'A <b>&');
        const box = document.body.children[0].children[0];
        r.hint = box.querySelector('[data-step="files"] .hint').textContent;
        r.bold = box.querySelectorAll('.hint b').map(b => b.textContent);
        r.slots = box.querySelectorAll('.fslot').map(s =>
          [s.querySelector('.fname') ? s.querySelector('.fname').value : null, s.querySelector('textarea').value]);
        // With no prefill, the popup is as it was: two empty slots.
        openAddApp();
        const plain = document.body.children[1].children[0];
        r.plain = [plain.querySelector('h4').textContent,
                   plain.querySelectorAll('.fslot').map(s => s.querySelector('textarea').value)];
    """)
    r = _node(script)
    assert r["hint"].startswith("A copy of A <b>&: its files")
    assert r["bold"] == ["A <b>&"]
    assert r["slots"] == [[None, "id: x <b>"], [None, ""], ["<i>.txt", "<b>bold</b> &amp;"]]
    assert r["plain"] == ["Add an application", ["", ""]]


# ---------- App Update: an app's files replaced, its data kept ----------
#
# Update, in an open app row, beside Duplicate: the app's files read back
# as Duplicate reads them, and the Add popup opened on them in its update
# mode - the id locked, no tab to pick, and Review the confirm ("Update
# <name>? This replaces the app's files. Its data is kept.") - whose
# Update PUTs them to /api/apps/<id>. The page's own appPanel and
# openAddApp run under node over the parsing DOM, against the files the
# server hands out; what the popup sends is then PUT to the server.

UPDATE_HARNESS = r"""
    // An app's open row, and its Update clicked: what it read and said,
    // the popup it opened, edited by `edit`, then Review - nothing sent
    // yet - and, if Review lets it, Update.
    async function updateApp(id, edit){
      const row = appPanel(APPS_.find(a => a.id===id));
      const btn = row.querySelector('[data-act="update"]');
      const before = document.body.children.length;
      const pending = btn.onclick();
      const out = {busy:btn.disabled};
      await pending;
      out.after = btn.disabled;
      out.reads = reads.splice(0);
      out.msg = row.querySelector('.a-msg').textContent;
      if(document.body.children.length===before){ out.popup = null; return out; }
      const bg = document.body.children[document.body.children.length-1], box = bg.children[0];
      const q = s => box.querySelector(s);
      out.popup = {
        title: q('h4').textContent,
        hint: q('[data-step="files"] .hint').textContent.replace(/\s+/g, ' '),
        locked: [q('[data-f="locked"] code').textContent, q('[data-f="locked"] .fslot-tag').textContent],
        slots: box.querySelectorAll('.fslot').map(s =>
          [s.dataset.file || '', s.querySelector('.fname') ? s.querySelector('.fname').value : null,
           s.querySelector('textarea').value]),
      };
      if(edit) edit(box);
      q('[data-act="review"]').onclick();
      const sure = q('[data-f="sure"]'), go = q('[data-act="install"]');
      out.review = {
        id: q('[data-f="id"]').textContent, problem: q('[data-f="msg"]').textContent,
        files: box.querySelectorAll('[data-f="files"] li').map(li => li.textContent),
        notice: !q('[data-f="notice"]').hidden, picker: !q('[data-f="tabField"]').hidden,
        sure: sure.hidden ? null : [sure.querySelector('h5').textContent, sure.querySelector('p').textContent],
        button: [go.textContent, !go.hidden, !go.disabled],
        sent: updates.length + installs.length,
      };
      if(!go.disabled) await go.onclick();
      out.updates = updates.splice(0);
      out.installs = installs.splice(0);
      out.installed = installed.splice(0);
      out.closed = !bg.parent;
      out.after_msg = q('[data-f="msg"]').textContent;
      out.button = [go.textContent, !go.disabled];
      return out;
    }
"""


def _run_update(client, scenario):
    listed, files = _served(client)
    return _node(_dupe_script(_page(client), listed, files, UPDATE_HARNESS + scenario))


# Home with two tabs: an install would be asked which; an update isn't.
TWO_TABS = "home.layout = {tabs:[{id:'home', name:'Home'}, {id:'two', name:'Two'}], default_tab:'home'};"


def test_an_open_app_row_has_update_before_duplicate(client):
    page = _page(client)
    panel = _js_function(page, "appPanel")
    # A plain button, Duplicate's style, to its left - opening a popup.
    assert re.search(
        r'<button class="btn sm" data-act="update" type="button" aria-haspopup="dialog">Update</button>\s*'
        r'<button class="btn sm" data-act="duplicate" type="button" aria-haspopup="dialog">Duplicate</button>',
        panel,
    )
    # It reads every file back, as Duplicate does, and opens the Add popup
    # on them, for this app - it writes nothing itself.
    handler = panel[panel.index("upd.onclick = async () => {") : panel.index("const dupe = act('duplicate');")]
    assert "const current = await readBack(upd, 'update');" in handler
    assert "if(current) openAddApp(current, null, a);" in handler
    for write in ("installApp", "updateApp", "uninstallApp", "loadApps", "loadHome", "bumpManifest"):
        assert write not in handler, write
    # The popup's update mode: its title, the id locked and shown as text,
    # Review held to the app's id and asking to confirm, Update PUTting.
    popup = _js_function(page, "openAddApp")
    assert "update ? 'Update application' : given ? 'Duplicate application' : 'Add an application'" in popup
    assert '<p class="install-id" data-f="locked">Application: <code>${esc(update.id)}</code>' in popup
    assert '<div class="danger-panel update-sure" data-f="sure">' in popup
    assert "<h5>Update ${esc(updName)}?</h5>" in popup
    assert "<p>This replaces the app's files. Its data is kept.</p>" in popup
    assert "if(update) f('sure').hidden = !read.id;" in popup
    assert "api.updateApp(update.id, body)" in popup
    # Its id isn't a field: there's none to edit, only app.yaml's line.
    assert "data-key" not in popup and "id-field" not in popup


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_update_opens_the_popup_on_all_the_apps_files_and_confirms_before_a_put(client, dupe_apps):
    (dupe_apps / "steps" / "config.json").write_text('{"goal": 8000}\n')
    original = {p.name: p.read_text() for p in (dupe_apps / "steps").iterdir() if p.is_file()}
    r = _run_update(client, TWO_TABS + "r.out = await updateApp('steps');")
    out = r["out"]

    # Every file read, the button off while they're read.
    assert out["reads"] == [["steps", f] for f in ("app.py", "app.yaml", "config.json", "empty.txt", "notes.txt")]
    assert (out["busy"], out["after"], out["msg"]) == (True, False, "")
    # The popup: titled for an update, the id locked - shown, not a field -
    # and every file in its slot as installed, app.yaml untouched.
    popup = out["popup"]
    assert popup["title"] == "Update application"
    assert popup["hint"].startswith("The files of Steps, as installed.")
    assert "a file whose slot you remove is deleted" in popup["hint"]
    assert popup["locked"] == ["steps", "Locked"]
    assert popup["slots"] == [
        ["app.yaml", None, original["app.yaml"]],
        ["app.py", None, original["app.py"]],
        ["", "config.json", '{"goal": 8000}\n'],
        ["", "empty.txt", ""],
        ["", "notes.txt", "stride: 0.75 m\n"],
    ]
    # Review is the confirm: what's applied, asked before it's sent - and
    # no tab to pick, though Home has two (the app is placed already).
    assert out["review"] == {
        "id": "Update application: steps", "problem": "",
        "files": ["app.yaml", "app.py", "config.json", "empty.txt", "notes.txt"],
        "notice": True, "picker": False,
        "sure": ["Update Steps?", "This replaces the app's files. Its data is kept."],
        "button": ["Update", True, True], "sent": 0,
    }
    # Update: the files PUT for this app - not installed - and the list
    # and Home re-read once it's done.
    [(app_id, body)] = out["updates"]
    assert app_id == "steps" and body == original
    assert out["installs"] == []
    assert out["installed"] == [[{"ok": True, "id": "steps", "has_app_py": True}, "Updated"]]
    assert out["closed"] is True

    # ... which the server takes: the same app, its files as sent.
    resp = client.put("/api/apps/steps", json={"files": body})
    assert resp.status_code == 200 and resp.get_json()["id"] == "steps"
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["pics", "steps", "steps-metres"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_an_update_sends_the_popup_as_edited_and_a_removed_slot_is_deleted(client, dupe_apps):
    r = _run_update(client, """
        r.out = await updateApp('steps', box => {
          box.querySelector('[data-file="app.py"] textarea').value += '# v2\\n';
          const notes = box.querySelectorAll('.fslot').find(s => s.querySelector('.fname') &&
                                                               s.querySelector('.fname').value === 'notes.txt');
          notes.querySelector('[data-act="remove"]').onclick();
        });
    """)
    out = r["out"]
    assert out["review"]["files"] == ["app.yaml", "app.py", "empty.txt"]
    [(app_id, body)] = out["updates"]
    assert sorted(body) == ["app.py", "app.yaml", "empty.txt"]
    assert body["app.py"].endswith("# v2\n")

    # The server replaces the folder with exactly that: notes.txt is gone.
    assert client.put(f"/api/apps/{app_id}", json={"files": body}).status_code == 200
    assert apps.app_files(str(dupe_apps / "steps")) == ["app.py", "app.yaml", "empty.txt"]
    assert (dupe_apps / "steps" / "app.py").read_text() == body["app.py"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_an_update_whose_id_changed_is_stopped_at_review(client, dupe_apps):
    r = _run_update(client, """
        r.out = await updateApp('steps', box => {
          const yaml = box.querySelector('[data-file="app.yaml"] textarea');
          yaml.value = yaml.value.replace('id: steps', 'id: strides');
        });
    """)
    review = r["out"]["review"]
    # Said, the Update off and no confirm asked - nothing sent.
    assert review["id"] == (
        'This updates "steps", but app.yaml\'s id is "strides": an update keeps the app\'s id. '
        "Duplicate makes a copy under a new one."
    )
    assert review["sure"] is None and review["button"] == ["Update", True, False]
    assert r["out"]["updates"] == [] and r["out"]["installs"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_refused_update_stays_in_the_popup(client, dupe_apps):
    r = _run_update(client, """
        updateReply = {ok:false, error:"the update doesn't load, so 'steps' is unchanged: app.py failed to import"};
        r.out = await updateApp('steps');
        updateReply = null;
        r.pics = await updateApp('pics');
    """)
    out = r["out"]
    # The server's refusal, as given; the popup open, Update back on.
    assert len(out["updates"]) == 1 and out["installed"] == [] and out["closed"] is False
    assert out["after_msg"] == "the update doesn't load, so 'steps' is unchanged: app.py failed to import"
    assert out["button"] == ["Update", True]
    # An app with a file that isn't text can't be updated from here: its
    # files can't all be read back, so the popup never opens.
    pics = r["pics"]
    assert pics["popup"] is None
    assert pics["msg"] == "Couldn't read logo.png ('logo.png' isn't text, so it can't be shown) — can't update Pics."


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_update_popup_sets_names_and_ids_as_text_never_markup(client):
    page = _page(client)
    script = _dupe_script(page, [], {}, """
        openAddApp([{name:'app.yaml', text:'id: a<b>\\nsizes: [{w: 1, h: 1}]\\n'}], null, {id:'a<b>', name:'A <b>&'});
        const box = document.body.children[0].children[0];
        box.querySelector('[data-act="review"]').onclick();
        r.hint = box.querySelector('[data-step="files"] .hint b').textContent;
        r.locked = box.querySelector('[data-f="locked"] code').textContent;
        r.id = box.querySelector('[data-f="id"] code').textContent;
        r.sure = box.querySelector('[data-f="sure"] h5').textContent;
    """)
    assert _node(script) == {"hint": "A <b>&", "locked": "a<b>", "id": "a<b>", "sure": "Update A <b>&?"}


# (an app.yaml, the id of the app it updates): the page's check, as the
# server's - an update keeps the app's id, whatever its form.
UPDATE_IDS = [
    ("id: echo\nname: Echo\n", "echo"),
    ("name: Echo\nid: 'echo'\n", "echo"),
    ('id: "echo"\n', "echo"),
    ("id: echo # a comment\n", "echo"),
    ("---\nid: echo\n", "echo"),
    ("id: echo\r\nname: CRLF\r\n", "echo"),
    ("id: other\nid: echo\n", "echo"),
    ("id: My_App\n", "My_App"),
    ("id: my_app\n", "My_App"),
    ("id: other\n", "echo"),
    ("id: echo-2\n", "echo"),
    ("name: No id\n", "echo"),
    ("settings:\n  id: echo\n", "echo"),
    ("id:\n", "echo"),
    ("id: ''\n", "echo"),
    ("id: 'unclosed\n", "echo"),
    ("", "echo"),
    ("   \n", "echo"),
]


def _server_update_id(text, keep):
    try:
        return apps._update_id(apps._manifest_to_install({"app.yaml": text}), keep)
    except apps.InstallError:
        return None


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_update_id_check_mirrors_the_servers(client):
    page = _page(client)
    script = "\n".join([_js(page, "NO_ID"), _js(page, "yamlId"), _js(page, "updateId")]) + f"""
        console.log(JSON.stringify({json.dumps(UPDATE_IDS)}.map(([text, keep]) => updateId(text, keep))));
    """
    result = _node(script)
    for (text, keep), read in zip(UPDATE_IDS, result):
        assert read.get("id") == _server_update_id(text, keep), repr(text)
        assert bool(read.get("problem")) == (_server_update_id(text, keep) is None), repr(text)


# ---------- multi-tab Home, slice 5: deploy-to-tab ----------
#
# The Add popup's Review asks which of Home's tabs the app goes on - only
# when there's more than one, the layout's default tab picked to start
# with. On Install a tab other than the default has the new app's
# instance - which the server's read has already put on the end of the
# default tab - lifted onto it, and the layout saved. The page's own
# openAddApp and placeNewAppOnTab run under node over the parsing DOM,
# against layouts the server hands out; what the page saves is then PUT
# to the server.


def test_the_add_popup_offers_a_tab_picker_wired_to_placement(client):
    page = _page(client)
    popup = _js_function(page, "openAddApp")
    # On Review, under the id: a field for the tab, hidden until there's a
    # choice - the modal's own field and select styling, no CSS of its own.
    assert re.search(
        r'<p class="install-id" data-f="id"></p>\s*'
        r'<div class="field install-tab" data-f="tabField" hidden>\s*'
        r'<label for="installTab">Add to tab</label>\s*'
        r'<select class="input" id="installTab" data-f="tab"></select>\s*'
        r"</div>",
        popup,
    )
    assert ".install-tab" not in page
    # ... and hidden means hidden: .field's display would show it otherwise.
    assert ".field[hidden]{display:none;}" in page
    # Review fills it from Home's layout - only with more than one tab and
    # not mid-Configure, the default selected - every id and name escaped.
    review = popup[popup.index("act('review').onclick") : popup.index("act('back').onclick")]
    assert "const tabs = home.layout && Array.isArray(home.layout.tabs) ? home.layout.tabs : [];" in review
    assert "if(tabs.length > 1 && !home.configuring && !update){" in review
    assert (
        "`<option value=\"${esc(t.id)}\"${t.id===home.layout.default_tab ? ' selected' : ''}>"
        "${esc(t.name)}</option>`"
    ) in review
    assert "f('tabField').hidden = false;" in review and "f('tabField').hidden = true;" in review
    # Install reads the choice - none when it wasn't asked - and, the popup
    # closed, places the app before Home and the list are re-read.
    install = popup[popup.index("install.onclick") :]
    assert "const tabId = f('tabField').hidden ? null : f('tab').value;" in install
    assert (
        "close();\n        if(tabId) await placeNewAppOnTab(res.id, tabId);\n"
        "        appInstalled(res, update ? 'Updated' : 'Installed');"
    ) in install
    # Placement: the layout read, the new app's instance lifted onto the
    # chosen tab and the layout saved - nothing to do for the default, and
    # nothing more if the read fails. Home's working copy is never touched.
    place = _js_function(page, "placeNewAppOnTab")
    assert "try{ layout = await api.homeLayout(); }\n    catch(e){ return; }" in place
    assert "tabId === layout.default_tab) return;" in place
    assert "inst = tab.items.splice(i, 1)[0]; break;" in place
    assert "to.items.push(inst);" in place
    assert "await api.saveLayout(layout);" in place
    assert "home." not in place


# The parts of the page an install goes through: the popup, Home's state
# (its layout, for the picker) and the placement.
ADD_PARTS = [
    "$", "el", "home", "openModal", "INSTALL_ID", "CUSTOM_CODE", "fileNameProblem", "NO_ID", "yamlId",
    "manifestId", "updateId", "fileSlot", "slotFiles", "openAddApp", "placeNewAppOnTab",
]

ADD_HARNESS = r"""
    const setTimeout = () => 0, clearTimeout = () => {};
    const copy = x => JSON.parse(JSON.stringify(x));
    // The server, as the test left it: the layout its read hands out now
    // the app's installed. Every install, read and save is noted; a save
    // is stored, and read back from then on.
    let served = SERVED;
    const installs = [], reads = [], saves = [], installed = [];
    const api = {
      installApp: async files => {
        installs.push(copy(files));
        return {ok:true, id:manifestId(files['app.yaml']).id};
      },
      homeLayout: async () => { reads.push(1); return copy(served); },
      saveLayout: async layout => { served = copy(layout); saves.push(copy(layout)); return {ok:true}; },
    };
    // Home and the list re-read - noting how many saves came first.
    const appInstalled = res => { installed.push({res, saves:saves.length}); };
    const r = {};

    // Add Application with Home's layout as `layout`: the app.yaml pasted,
    // Review - the picker as shown - then the tab `pick`ed, as a browser
    // sets the select (no pick: the option marked selected, else the
    // first), and Install.
    async function add(layout, yaml, pick){
      home.layout = copy(layout);
      openAddApp();
      const bg = document.body.children[document.body.children.length-1], box = bg.children[0];
      const q = s => box.querySelector(s);
      q('[data-file="app.yaml"] textarea').value = yaml;
      q('[data-act="review"]').onclick();
      const field = q('[data-f="tabField"]'), select = q('[data-f="tab"]');
      const options = select.querySelectorAll('option');
      const out = {
        id: q('[data-f="id"]').textContent,
        picker: field.hidden ? null : options.map(o => [o.attrs.value, o.textContent, 'selected' in o.attrs]),
      };
      const marked = options.find(o => 'selected' in o.attrs) || options[0];
      select.value = pick !== undefined ? pick : marked ? marked.attrs.value : '';
      await q('[data-act="install"]').onclick();
      out.installs = installs.splice(0).length;
      out.reads = reads.splice(0).length;
      out.saves = saves.splice(0);
      out.installed = installed.splice(0);
      out.closed = !bg.parent;
      return out;
    }
"""


def _add_script(page, served, scenario):
    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)
    harness = ADD_HARNESS.replace("served = SERVED", f"served = {json.dumps(served)}")
    return (
        PARSING_DOM + esc + "\n" + "\n".join(_js(page, n) for n in ADD_PARTS) + harness
        + "(async () => {\n" + scenario + "\nconsole.log(JSON.stringify(r));\n})()"
        + ".catch(e => { console.error(e); process.exit(1); });"
    )


NEW_APP = "id: e\nname: App E\nsizes: [{w: 2, h: 1}, {w: 1, h: 1}]\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_add_popups_tab_picker_shows_only_with_two_tabs(client, tab_apps):
    page = _page(client)
    # Home and Two, Home the default; the same with Two the default; one tab.
    _, two = _seeded(client)
    _, second = _seeded(client, {**SEEDED, "default_tab": "two"})
    _, one = _seeded(client, {"tabs": [_tab("home", "Home", _instance("ia", "a"))], "default_tab": "home"})
    r = _node(_add_script(page, two, f"""
        r.two = await add({json.dumps(two)}, {json.dumps(NEW_APP)});
        served = {json.dumps(second)};
        r.second = await add({json.dumps(second)}, {json.dumps(NEW_APP)});
        served = {json.dumps(one)};
        r.one = await add({json.dumps(one)}, {json.dumps(NEW_APP)});
        // Home not read yet: no layout to pick from.
        r.unread = await add(null, {json.dumps(NEW_APP)});
    """))
    # Two tabs: an option a tab, in the layout's order, by name (as text),
    # the default selected - not merely the first.
    assert r["two"]["picker"] == [["home", "Home", True], ["two", "Two <b>", False]]
    assert r["second"]["picker"] == [["home", "Home", False], ["two", "Two <b>", True]]
    # One tab, or none read: no picker, and nothing read or saved - it
    # lands on the default, as before the picker.
    for out in (r["one"], r["unread"]):
        assert out["picker"] is None
        assert (out["reads"], out["saves"]) == (0, [])
    # Every time: installed, the popup closed, Home and the list re-read.
    for out in r.values():
        assert out["id"] == "Install application: e"
        assert out["installs"] == 1 and out["closed"] is True
        assert out["installed"] == [{"res": {"ok": True, "id": "e"}, "saves": 0}]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_tab_picker_is_hidden_while_configuring(client, tab_apps):
    page = _page(client)
    # The guard is on both counts: more than one tab, and not mid-Configure
    # (nor an update: its app is placed already).
    assert "if(tabs.length > 1 && !home.configuring && !update){" in _js_function(page, "openAddApp")
    _, two = _seeded(client)
    r = _node(_add_script(page, two, f"""
        // Two tabs, but Configure is on: no picker. Placing now would be a
        // server save that the Configure-off save then overwrites from the
        // working copy, losing the choice - so the app joins the tab on
        // screen and is moved by hand, like any tile, in Configure.
        home.configuring = true;
        r.on = await add({json.dumps(two)}, {json.dumps(NEW_APP)});
        // Configure off: the picker is back, the slice's normal behaviour.
        home.configuring = false;
        r.off = await add({json.dumps(two)}, {json.dumps(NEW_APP)});
    """))
    # Mid-Configure: no picker, nothing read or saved - install alone.
    assert r["on"]["picker"] is None
    assert (r["on"]["reads"], r["on"]["saves"]) == (0, [])
    assert r["on"]["installs"] == 1 and r["on"]["closed"] is True
    assert r["on"]["installed"] == [{"res": {"ok": True, "id": "e"}, "saves": 0}]
    # Off again: the picker returns, the default selected.
    assert r["off"]["picker"] == [["home", "Home", True], ["two", "Two <b>", False]]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_installing_onto_a_chosen_tab_moves_the_new_app_and_saves_once(client, tab_apps):
    page = _page(client)
    # Home (the default): a, b, then d, placed nowhere; Two: c.
    _, before = _seeded(client)
    # The app installed - as the popup's POST does - and the server's read
    # now has its instance on the end of the default tab.
    assert client.post("/api/apps", json={"files": {"app.yaml": NEW_APP}}).status_code == 201
    after = client.get("/api/home-layout").get_json()
    assert [[i["app_id"] for i in t["items"]] for t in after["tabs"]] == [["a", "b", "d", "e"], ["c"]]
    new = after["tabs"][0]["items"][-1]
    assert new["size"] == {"w": 2, "h": 1} and new["config"] == {}

    r = _node(_add_script(page, after, f"""
        r.kept = await add({json.dumps(before)}, {json.dumps(NEW_APP)});
        r.moved = await add({json.dumps(before)}, {json.dumps(NEW_APP)}, 'two');
        // A read that fails leaves it where the server put it.
        api.homeLayout = async () => {{ throw new Error('down'); }};
        r.failed = await add({json.dumps(before)}, {json.dumps(NEW_APP)}, 'two');
    """))

    # The default taken: read, nothing to move, nothing saved.
    kept = r["kept"]
    assert kept["picker"] == [["home", "Home", True], ["two", "Two <b>", False]]
    assert (kept["reads"], kept["saves"]) == (1, [])
    assert kept["installed"] == [{"res": {"ok": True, "id": "e"}, "saves": 0}]
    # Two picked: the layout the server read, with the new instance - id,
    # size and config intact - lifted off the default onto the end of Two,
    # saved once, whole, before Home is re-read.
    moved = r["moved"]
    want = json.loads(json.dumps(after))
    want["tabs"][1]["items"].append(want["tabs"][0]["items"].pop())
    assert moved["reads"] == 1 and moved["saves"] == [want]
    assert want["tabs"][1]["items"][-1] == new
    assert moved["installed"] == [{"res": {"ok": True, "id": "e"}, "saves": 1}]
    # The read failing: no save, and Home re-read all the same.
    failed = r["failed"]
    assert failed["saves"] == []
    assert failed["installed"] == [{"res": {"ok": True, "id": "e"}, "saves": 0}]
    assert all(out["closed"] for out in r.values())

    # What the page saved, stored: the app on Two, under the instance id
    # the read gave it - and still there on every read after (a reload).
    stored = _stored(client, moved["saves"][0])
    assert [[i["app_id"] for i in t["items"]] for t in stored["tabs"]] == [["a", "b", "d"], ["c", "e"]]
    assert stored["tabs"][1]["items"][-1] == new
    assert stored["default_tab"] == "home"
    assert client.get("/api/home-layout").get_json() == stored


# ---------- apps as sources: the input element ----------
#
# An app's tile can carry an input: a labelled field and Save. Save sends
# the app its action (POST /api/app/<id>/action) with {key: what was
# typed} and the tile's config, and the tile is redrawn from the spec the
# reply carries - or the refusal shown on the tile. The page's own spec
# renderer runs under node over the stand-in DOM, against the replies the
# server gives.

INPUT_PARTS = ["el", "SPECS", "renderSpec", "UPLOAD_MAX", "specInput", "specMsg", "confirmFirst"]

INPUT_HARNESS = r"""
    // What Save reaches: the action route (each call noted, the reply the
    // test's), the tile's config, and the tile's redraw.
    // A file input's Save reaches its own sender, noted apart - the file
    // by its name, and a payload only when one is sent.
    const calls = [], fileCalls = [], repaints = [];
    let reply = {ok:true, spec:{type:'empty', message:'fresh'}};
    let release = null;
    const api = {
      appAction: (id, action, payload, cfg) => {
        calls.push([id, action, payload, cfg]);
        return new Promise(done => { release = () => done(reply); });
      },
      appActionFile: (id, action, key, file, cfg, payload) => {
        fileCalls.push(payload===undefined ? [id, action, key, file.name, cfg] : [id, action, key, file.name, cfg, payload]);
        return new Promise(done => { release = () => done(reply); });
      },
    };
    // The modal chrome confirmFirst() opens: its markup noted, the parts
    // it fills found in it, and closed by a button - or by openModal's
    // own close, as Escape or a click outside closes it.
    const modals = [];
    function openModal(html, onClose){
      const found = {};
      for(const [, k, v] of html.matchAll(/data-(f|act)="([\w-]+)"/g)) found[`[data-${k}="${v}"]`] = mk(k==='f' ? 'p' : 'button');
      const modal = {html, found, closed:false};
      const close = () => { if(modal.closed) return; modal.closed = true; if(onClose) onClose(); };
      modal.dismiss = close;
      modals.push(modal);
      return {box:{querySelector: sel => found[sel] || null}, close};
    }
    const tick = () => new Promise(done => setImmediate(done));
    const tileCfg = id => ({for:id});
    const repaintTile = (id, spec) => repaints.push([id, spec]);
    // What the other specs draw with, which these tests don't look at.
    const sparkline = () => mk('div'), fmtSpecValue = v => String(v);

    // The input's parts, as drawn: its label, its field, unit and Save,
    // and its message line.
    const parts = box => {
      const label = box.children.find(c => c.className==='t');
      const row = box.children.find(c => c.className==='row');
      const field = row.children.find(c => c.tag==='input' || c.tag==='select');
      const unit = row.children.find(c => c.className==='u');
      const save = row.children.find(c => c.tag==='button');
      const msg = box.children.find(c => c.className==='m');
      return {label, field, unit, save, msg};
    };
    const look = box => {
      const {label, field, unit, save, msg} = parts(box);
      return {cls:box.className, kids:box.children.map(c => c.className),
        label:label ? label.textContent : null, field:[field.className, field.type, field.step || null, field.attrs['aria-label']],
        unit:unit ? unit.textContent : null, save:[save.className, save.type, save.textContent, !!save.disabled],
        msg:[msg.textContent, msg.attrs.role], html:[box, ...box.querySelectorAll('*')].map(n => n._html).join('')};
    };
    // Save pressed - or Enter in the field - with `typed` in it, and the
    // reply let through: what was sent, what the tile shows meanwhile and
    // after.
    async function save(box, typed, how){
      const {field, save:btn, msg} = parts(box);
      field.value = typed;
      const pending = how==='enter' ? field.onkeydown({key:'Enter'}) : btn.onclick();
      const busy = [!!btn.disabled, !!field.disabled];
      if(release){ release(); release = null; }
      await pending;
      return {busy, after:[!!btn.disabled, !!field.disabled], value:field.value, msg:msg.textContent,
        calls:calls.splice(0), repaints:repaints.splice(0)};
    }
    // A file picker's toggle, as drawn: its label, its checkbox and text.
    const toggleOf = box => {
      const row = box.children.find(c => c.className==='row');
      const wrap = row.children.find(c => c.className==='tg');
      return wrap ? {wrap, check:wrap.children[0], text:wrap.children[1]} : null;
    };
    // A file picker with a toggle: `files` picked, the box `ticked` or
    // not, Save pressed - and a confirm, if one opens, answered: 'confirm',
    // 'cancel', or 'dismiss' (Escape, a click outside). What it asked,
    // what was held while it asked, and what was sent.
    async function saveToggled(box, files, ticked, answer){
      const {field, save:btn, msg} = parts(box);
      const {check} = toggleOf(box);
      field.files = files;
      check.checked = ticked;
      const opened = modals.length;
      const pending = btn.onclick();
      const modal = modals.length > opened ? modals[modals.length-1] : null;
      const asked = modal ? {title:modal.found['[data-f="title"]'].textContent, what:modal.found['[data-f="what"]'].textContent,
                             html:modal.html.replace(/\s+/g, ' ').trim()} : null;
      const held = [!!btn.disabled, !!field.disabled, !!check.disabled];
      const sentWhileAsking = fileCalls.length;
      if(modal){
        if(answer==='dismiss') modal.dismiss();
        else modal.found[`[data-act="${answer}"]`].onclick();
      }
      await tick();
      if(release){ release(); release = null; }
      await pending;
      return {asked, held, sentWhileAsking, closed:modal ? modal.closed : null,
        after:[!!btn.disabled, !!field.disabled, !!check.disabled], ticked:!!check.checked, msg:msg.textContent,
        sent:fileCalls.splice(0), repaints:repaints.splice(0)};
    }
    // The same for a file picker, with `files` picked in it: Save only.
    async function saveFile(box, files){
      const {field, save:btn, msg} = parts(box);
      field.files = files;
      const pending = btn.onclick();
      const busy = [!!btn.disabled, !!field.disabled];
      if(release){ release(); release = null; }
      await pending;
      return {busy, after:[!!btn.disabled, !!field.disabled], picked:(field.files || []).map(f => f.name),
        msg:msg.textContent, calls:calls.splice(0), sent:fileCalls.splice(0), repaints:repaints.splice(0)};
    }
    const r = {};
"""


def _input_script(page, scenario):
    return (
        MINI_DOM + "\n".join(_js(page, n) for n in INPUT_PARTS) + INPUT_HARNESS
        + "(async () => {\n" + scenario + "\nconsole.log(JSON.stringify(r));\n})()"
        + ".catch(e => { console.error(e); process.exit(1); });"
    )


WEIGHT_FIELD = {"type": "input", "key": "weight_kg", "action": "log",
                "label": "Log <b>weight</b>", "kind": "number", "unit": "kg"}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_an_input_draws_a_labelled_field_and_save_as_text(client):
    page = _page(client)
    spec = {"type": "panel", "children": [{"type": "number", "value": 81.2, "unit": "kg"}, WEIGHT_FIELD]}
    r = _node(_input_script(page, f"""
        const panel = renderSpec({json.dumps(spec)}, 'Weight', 'weight');
        r.panel = panel.children.map(c => c.className);
        r.input = look(panel.children[1]);
        r.text = look(renderSpec({json.dumps({"type": "input", "key": "note", "action": "save", "kind": "text"})}, 'Notes', 'notes'));
        r.unknownKind = look(renderSpec({json.dumps({"type": "input", "key": "k", "action": "a", "kind": "date"})}, 'X', 'x')).field;
    """))

    # In a panel, under the number: the label over the field, its unit and
    # Save - all of it text, the label's markup included - and an empty
    # message line for a refusal.
    assert r["panel"] == ["spec-number", "spec-input"]
    assert r["input"] == {
        "cls": "spec-input", "kids": ["t", "row", "m"], "label": "Log <b>weight</b>",
        "field": ["input", "number", "any", "Log <b>weight</b>"], "unit": "kg",
        "save": ["btn sm", "button", "Save", False], "msg": ["", "status"], "html": "",
    }
    # A text field: no step, no unit, no label - named by its key.
    assert r["text"]["kids"] == ["row", "m"]
    assert r["text"]["field"] == ["input", "text", None, "note"]
    assert r["text"]["unit"] is None
    # Only "text" is text: any other kind is a number, the builder's default.
    assert r["unknownKind"][1] == "number"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_save_sends_the_action_and_redraws_the_tile_from_the_reply(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = () => renderSpec({{type:'panel', children:[{json.dumps(WEIGHT_FIELD)}]}}, 'Weight', 'weight').children[0];
        r.clicked = await save(box(), '81.2');
        r.entered = await save(box(), ' 80.9 ', 'enter');
        r.text = await save(renderSpec({json.dumps({"type": "input", "key": "note", "action": "save", "kind": "text"})}, 'Notes', 'notes'), '  hi  ');
    """))

    fresh = {"type": "empty", "message": "fresh"}
    # The app's action, {key: the number typed}, with the tile's config;
    # Save and the field held while it's on its way; the tile redrawn from
    # the spec the reply carries.
    assert r["clicked"] == {
        "busy": [True, True], "after": [False, False], "value": "81.2", "msg": "",
        "calls": [["weight", "log", {"weight_kg": 81.2}, {"for": "weight"}]],
        "repaints": [["weight", fresh]],
    }
    assert r["entered"]["calls"] == [["weight", "log", {"weight_kg": 80.9}, {"for": "weight"}]]
    assert r["entered"]["repaints"] == [["weight", fresh]]
    # Text is sent as typed.
    assert r["text"]["calls"] == [["notes", "save", {"note": "  hi  "}, {"for": "notes"}]]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_refused_save_shows_why_on_the_tile(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = renderSpec({json.dumps(WEIGHT_FIELD)}, 'Weight', 'weight');
        reply = {{ok:false, error:'Enter your weight in kg, as a <b>positive</b> number'}};
        r.refused = await save(box, '-3');
        reply = {{ok:false}};
        r.unsaid = await save(box, '81');
        r.html = look(box).html;
    """))

    # The server's reason, as text, under the field - still as typed, Save
    # back - and the tile left as it was.
    assert r["refused"] == {
        "busy": [True, True], "after": [False, False], "value": "-3",
        "msg": "Enter your weight in kg, as a <b>positive</b> number",
        "calls": [["weight", "log", {"weight_kg": -3}, {"for": "weight"}]], "repaints": [],
    }
    assert r["unsaid"]["msg"] == "Couldn't save."
    assert r["html"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_nothing_is_sent_for_a_blank_field_or_a_tile_drawn_for_no_app(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = () => renderSpec({json.dumps(WEIGHT_FIELD)}, 'Weight', 'weight');
        r.blank = await save(box(), '   ');
        r.junk = await save(box(), 'abc');
        r.text = await save(renderSpec({json.dumps({"type": "input", "key": "note", "action": "save", "kind": "text"})}, 'Notes', 'notes'), ' ');
        const orphan = renderSpec({json.dumps(WEIGHT_FIELD)}, 'Weight');
        r.orphan = look(orphan).save;
        r.orphanSave = await save(orphan, '81');
    """))

    assert r["blank"]["msg"] == r["junk"]["msg"] == "Enter a number."
    assert r["text"]["msg"] == "Enter something to save."
    for case in ("blank", "junk", "text", "orphanSave"):
        assert r[case]["calls"] == [] and r[case]["repaints"] == [], case
    assert r["orphan"] == ["btn sm", "button", "Save", True]


MOOD_FIELD = {"type": "input", "key": "mood", "action": "log", "label": "Mood today",
              "kind": "select", "unit": None, "options": ["1", "2", "3", "4", "5"]}

# What a dropdown holds, in order: each option's value, its text, and
# whether it's disabled or selected.
CHOICES = r"""
    const choices = field => field.children.map(o => [o.tag, o.value, o.textContent, !!o.disabled, !!o.selected]);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_select_draws_a_dropdown_of_its_options_after_a_placeholder(client):
    page = _page(client)
    tagged = {"type": "input", "key": "tag", "action": "add", "kind": "select", "unit": "mg",
              "options": ["<b>bold</b>", "007", " a "]}
    r = _node(_input_script(page, CHOICES + f"""
        const panel = renderSpec({{type:'panel', children:[{{type:'empty', message:'No mood logged yet'}}, {json.dumps(MOOD_FIELD)}]}}, 'Mood', 'mood');
        r.panel = panel.children.map(c => c.className);
        const box = panel.children[1], {{field}} = parts(box);
        r.mood = look(box);
        r.tag = field.tag;
        r.choices = choices(field);
        const other = renderSpec({json.dumps(tagged)}, 'Tags', 'tags');
        r.other = look(other);
        r.otherChoices = choices(parts(other).field);
    """))

    # In a panel, at its foot: the label over the dropdown and Save, and
    # an empty message line - the number field's layout, the field a
    # <select> named by the label.
    assert r["panel"] == ["spec-msg", "spec-input"]
    assert r["tag"] == "select"
    assert r["mood"]["kids"] == ["t", "row", "m"]
    assert r["mood"]["label"] == "Mood today"
    assert r["mood"]["field"] == ["input", None, None, "Mood today"]
    assert r["mood"]["unit"] is None
    assert r["mood"]["save"] == ["btn sm", "button", "Save", False]
    # A placeholder first - chosen, but disabled so it can't be chosen
    # back - then one option per entry, in order, value and text alike.
    assert r["choices"] == [
        ["option", "", "Choose…", True, True],
        *[["option", v, v, False, False] for v in ["1", "2", "3", "4", "5"]],
    ]
    # Options are text, as written - markup and spaces included; no label
    # means it's named by its key, and a unit sits beside it as ever.
    assert r["otherChoices"][1:] == [["option", v, v, False, False] for v in ["<b>bold</b>", "007", " a "]]
    assert r["other"]["field"][3] == "tag"
    assert r["other"]["unit"] == "mg"
    assert r["mood"]["html"] == r["other"]["html"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_select_sends_the_chosen_option_as_written(client):
    page = _page(client)
    zeros = {"type": "input", "key": "code", "action": "pick", "kind": "select", "options": ["007", "1e3"]}
    r = _node(_input_script(page, f"""
        const box = () => renderSpec({{type:'panel', children:[{json.dumps(MOOD_FIELD)}]}}, 'Mood', 'mood').children[0];
        r.clicked = await save(box(), '3');
        r.entered = await save(box(), '5', 'enter');
        r.zeros = await save(renderSpec({json.dumps(zeros)}, 'Codes', 'codes'), '007');
        r.e = await save(renderSpec({json.dumps(zeros)}, 'Codes', 'codes'), '1e3');
        const refused = box();
        reply = {{ok:false, error:'Choose a mood from 1 to 5'}};
        r.refused = await save(refused, '9');
    """))

    fresh = {"type": "empty", "message": "fresh"}
    # The app's action, {key: the option chosen} - the string, never
    # coerced to a number - with the tile's config; Save and the dropdown
    # held while it's on its way; the tile redrawn from the reply.
    assert r["clicked"] == {
        "busy": [True, True], "after": [False, False], "value": "3", "msg": "",
        "calls": [["mood", "log", {"mood": "3"}, {"for": "mood"}]],
        "repaints": [["mood", fresh]],
    }
    assert r["entered"]["calls"] == [["mood", "log", {"mood": "5"}, {"for": "mood"}]]
    assert r["zeros"]["calls"] == [["codes", "pick", {"code": "007"}, {"for": "codes"}]]
    assert r["e"]["calls"] == [["codes", "pick", {"code": "1e3"}, {"for": "codes"}]]
    # The app's refusal - its handle() holds the choice to its options,
    # not the host - under the dropdown, the tile left as it was.
    assert r["refused"]["calls"] == [["mood", "log", {"mood": "9"}, {"for": "mood"}]]
    assert r["refused"]["msg"] == "Choose a mood from 1 to 5"
    assert r["refused"]["repaints"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_an_unmade_choice_does_not_post(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = renderSpec({json.dumps(MOOD_FIELD)}, 'Mood', 'mood');
        // The placeholder still chosen: a <select>'s value is its value, ''.
        r.unmade = await save(box, '');
        r.thenChosen = await save(box, '2');
        const orphan = renderSpec({json.dumps(MOOD_FIELD)}, 'Mood');
        r.orphan = look(orphan).save;
        r.orphanSave = await save(orphan, '2');
    """))

    # Said on the tile, nothing sent - a log is always deliberate.
    assert r["unmade"]["msg"] == "Choose an option."
    assert r["unmade"]["calls"] == [] and r["unmade"]["repaints"] == []
    assert r["unmade"]["busy"] == [False, False]
    # Once one is chosen it goes, and the message clears.
    assert r["thenChosen"]["calls"] == [["mood", "log", {"mood": "2"}, {"for": "mood"}]]
    assert r["thenChosen"]["msg"] == ""
    # Drawn for no app, Save is off and sends nothing, as for any input.
    assert r["orphan"] == ["btn sm", "button", "Save", True]
    assert r["orphanSave"]["calls"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_number_and_text_inputs_are_still_fields_beside_the_select(client):
    page = _page(client)
    text = {"type": "input", "key": "note", "action": "save", "kind": "text", "options": None}
    r = _node(_input_script(page, f"""
        const weight = renderSpec({json.dumps(dict(WEIGHT_FIELD, options=None))}, 'Weight', 'weight');
        const note = renderSpec({json.dumps(text)}, 'Notes', 'notes');
        r.tags = [parts(weight).field.tag, parts(note).field.tag];
        r.kids = [parts(weight).field.children.length, parts(note).field.children.length];
        r.number = await save(weight, ' 80.5 ');
        r.junk = await save(weight, 'abc');
        r.text = await save(note, ' 3 ');
    """))

    # An <input>, no options in it, and a number still sent as a number,
    # text as typed - the select branch leaves them as they were.
    assert r["tags"] == ["input", "input"]
    assert r["kids"] == [0, 0]
    assert r["number"]["calls"] == [["weight", "log", {"weight_kg": 80.5}, {"for": "weight"}]]
    assert r["junk"]["msg"] == "Enter a number." and r["junk"]["calls"] == []
    assert r["text"]["calls"] == [["notes", "save", {"note": " 3 "}, {"for": "notes"}]]


def test_a_selects_dropdown_is_built_as_text_and_keeps_its_arrow(client):
    page = _page(client)
    spec_input = _js_function(page, "specInput")
    # Option text set as text, never markup, and the choice sent as it
    # is - Number() only for a number field.
    assert "const field = el(choice ? 'select' : 'input','input');" in spec_input
    assert "o.value = o.textContent = String(v);" in spec_input
    assert "const value = numeric ? Number(typed) : typed;" in spec_input
    assert "const numeric = !choice && !upload && s.kind!=='text';" in spec_input
    assert "if(choice && !typed){ msg.textContent = 'Choose an option.'; return; }" in spec_input
    # The tile's fields are drawn as plain text boxes; a select keeps its
    # native dropdown arrow, as the page's other selects do.
    assert ".spec-input select.input{appearance:auto;-moz-appearance:auto;}" in page


def test_the_input_reaches_the_action_route_through_the_api_block(client):
    page = _page(client)
    # The only place a URL lives: the action, its payload and the tile's
    # config, POSTed.
    assert (
        "    appAction:    (id, action, payload, cfg) =>\n"
        "      send('/api/app/'+enc(id)+'/action', 'POST', {action, payload, cfg: cfg || {}}),"
    ) in page
    spec_input = _js_function(page, "specInput")
    assert "api.appAction(appId, s.action, {[s.key]: value}, tileCfg(appId));" in spec_input
    assert "if(res.ok && res.spec) repaintTile(appId, res.spec);" in spec_input
    for markup in ("innerHTML", "${", "fetch(", "'/api/"):
        assert markup not in spec_input, markup
    # Drawn for its app - the id threaded from the tile, through a panel,
    # to the input.
    assert "tile.appendChild(renderSpec(t.spec, t.name, t.app_id));" in _js_function(page, "paintTiles")
    assert "input: (s, name, appId) => specInput(s, appId)," in page
    assert "box.appendChild(renderSpec(c, name, appId))" in page
    # In Configure a tile's face is inert, its input with it: Configure
    # arranges tiles, it doesn't act on them.
    assert ".tiles.configuring .tile > :not(.tile-cfg){pointer-events:none;}" in page


FILE_FIELD = {"type": "input", "key": "file", "action": "import", "label": "Import <b>weigh-ins</b>",
              "kind": "file", "unit": None, "options": None, "accept": ".txt,.csv"}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_file_input_draws_a_picker_offering_its_accept(client):
    page = _page(client)
    bare = {"type": "input", "key": "roster", "action": "load", "kind": "file", "unit": "ics",
            "options": None, "accept": None}
    r = _node(_input_script(page, f"""
        const panel = renderSpec({{type:'panel', children:[{{type:'empty', message:'No weigh-ins yet'}}, {json.dumps(FILE_FIELD)}]}}, 'Import', 'import');
        r.panel = panel.children.map(c => c.className);
        const box = panel.children[1], {{field}} = parts(box);
        r.look = look(box);
        r.picker = [field.tag, field.accept, field.children.length, typeof field.onkeydown];
        const other = renderSpec({json.dumps(bare)}, 'Rota', 'rota');
        r.other = look(other);
        r.otherAccept = 'accept' in parts(other).field;
    """))

    # At the panel's foot, the number field's layout: the label over the
    # picker and Save, then an empty message line - every part text.
    assert r["panel"] == ["spec-msg", "spec-input"]
    assert r["look"] == {
        "cls": "spec-input", "kids": ["t", "row", "m"], "label": "Import <b>weigh-ins</b>",
        "field": ["input", "file", None, "Import <b>weigh-ins</b>"], "unit": None,
        "save": ["btn sm", "button", "Save", False], "msg": ["", "status"], "html": "",
    }
    # An <input type="file"> offering the accept hints as written; Enter
    # opens a picker, so it's Save alone that sends.
    assert r["picker"] == ["input", ".txt,.csv", 0, "undefined"]
    # No accept, no hint - any file; named by its key, a unit beside it.
    assert r["otherAccept"] is False
    assert r["other"]["field"] == ["input", "file", None, "roster"]
    assert r["other"]["unit"] == "ics"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_file_input_sends_the_file_picked_and_redraws_the_tile(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = () => renderSpec({{type:'panel', children:[{json.dumps(FILE_FIELD)}]}}, 'Import', 'import').children[0];
        r.picked = await saveFile(box(), [{{name:'w.txt', size:16}}, {{name:'also.txt', size:1}}]);
        r.atCap = await saveFile(box(), [{{name:'big.db', size:UPLOAD_MAX}}]);
        const refused = box();
        reply = {{ok:false, error:"line 2: expected 'YYYY-MM-DD <kg>'"}};
        r.refused = await saveFile(refused, [{{name:'bad.txt', size:9}}]);
        r.html = look(refused).html;
    """))

    fresh = {"type": "empty", "message": "fresh"}
    # The first file picked, by its own sender - never the JSON one -
    # under the input's key with the action and the tile's config; Save
    # and the picker held while it's on its way; the tile redrawn from
    # the reply.
    assert r["picked"] == {
        "busy": [True, True], "after": [False, False], "picked": ["w.txt", "also.txt"], "msg": "",
        "calls": [], "sent": [["import", "import", "file", "w.txt", {"for": "import"}]],
        "repaints": [["import", fresh]],
    }
    # Up to the cap goes.
    assert r["atCap"]["sent"] == [["import", "import", "file", "big.db", {"for": "import"}]]
    # The app's refusal under the picker - the file still picked - and the
    # tile left as it was.
    assert r["refused"]["sent"] == [["import", "import", "file", "bad.txt", {"for": "import"}]]
    assert r["refused"]["msg"] == "line 2: expected 'YYYY-MM-DD <kg>'"
    assert r["refused"]["picked"] == ["bad.txt"]
    assert r["refused"]["after"] == [False, False]
    assert r["refused"]["repaints"] == []
    assert r["html"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_file_input_sends_nothing_with_no_file_or_one_over_the_cap(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = renderSpec({json.dumps(FILE_FIELD)}, 'Import', 'import');
        r.none = await saveFile(box, []);
        r.unset = await saveFile(box, undefined);
        r.big = await saveFile(box, [{{name:'huge.db', size:UPLOAD_MAX + 1}}]);
        r.thenPicked = await saveFile(box, [{{name:'w.txt', size:16}}]);
        const orphan = renderSpec({json.dumps(FILE_FIELD)}, 'Import');
        r.orphan = look(orphan).save;
        r.orphanSave = await saveFile(orphan, [{{name:'w.txt', size:16}}]);
    """))

    # Said on the tile, nothing sent, nothing held.
    for case in ("none", "unset"):
        assert r[case]["msg"] == "Choose a file.", case
    assert r["big"]["msg"] == "File too large (max 25 MB)."
    for case in ("none", "unset", "big", "orphanSave"):
        assert r[case]["calls"] == r[case]["sent"] == r[case]["repaints"] == [], case
    for case in ("none", "unset", "big"):
        assert r[case]["busy"] == [False, False], case
    # Once one is picked it goes, and the message clears.
    assert r["thenPicked"]["sent"] == [["import", "import", "file", "w.txt", {"for": "import"}]]
    assert r["thenPicked"]["msg"] == ""
    # Drawn for no app, Save is off and sends nothing, as for any input.
    assert r["orphan"] == ["btn sm", "button", "Save", True]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_number_text_and_select_still_post_json_beside_the_file(client):
    page = _page(client)
    text = {"type": "input", "key": "note", "action": "save", "kind": "text", "options": None, "accept": None}
    r = _node(_input_script(page, f"""
        r.number = await save(renderSpec({json.dumps(dict(WEIGHT_FIELD, options=None, accept=None))}, 'Weight', 'weight'), '80.5');
        r.text = await save(renderSpec({json.dumps(text)}, 'Notes', 'notes'), 'hi', 'enter');
        r.select = await save(renderSpec({json.dumps(dict(MOOD_FIELD, accept=None))}, 'Mood', 'mood'), '3');
        r.types = [{json.dumps(dict(WEIGHT_FIELD, accept=None))}, {json.dumps(text)}].map(s => parts(renderSpec(s, 'X', 'x')).field.type);
        r.fileCalls = fileCalls.length;
    """))

    # Each through the JSON sender, as before; the file sender never.
    assert r["number"]["calls"] == [["weight", "log", {"weight_kg": 80.5}, {"for": "weight"}]]
    assert r["text"]["calls"] == [["notes", "save", {"note": "hi"}, {"for": "notes"}]]
    assert r["select"]["calls"] == [["mood", "log", {"mood": "3"}, {"for": "mood"}]]
    assert r["types"] == ["number", "text"]
    assert r["fileCalls"] == 0


TOGGLED_FIELD = dict(
    FILE_FIELD, key="file", action="upload", label="Upload roster", accept=".ics",
    toggle={"key": "backfill", "label": "Back<b>fill</b>", "confirm": "Backfill <i>past</i> duties too?"},
)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_file_inputs_toggle_is_a_checkbox_in_its_row_before_save(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = renderSpec({{type:'panel', children:[{{type:'empty', message:'x'}}, {json.dumps(TOGGLED_FIELD)}]}}, 'Roster', 'roster').children[1];
        const row = box.children.find(c => c.className==='row');
        const tg = toggleOf(box);
        r.row = row.children.map(c => [c.tag, c.className]);
        r.toggle = [tg.wrap.tag, tg.check.tag, tg.check.type, !!tg.check.checked, tg.text.tag, tg.text.textContent];
        r.look = look(box);
        r.plain = renderSpec({json.dumps(FILE_FIELD)}, 'Import', 'import').children.find(c => c.className==='row').children.map(c => c.tag);
        // A toggle with no key is no toggle; one with no label is named by its key.
        r.keyless = toggleOf(renderSpec({json.dumps(dict(TOGGLED_FIELD, toggle={"label": "Backfill"}))}, 'R', 'r'));
        r.unlabelled = toggleOf(renderSpec({json.dumps(dict(TOGGLED_FIELD, toggle={"key": "backfill"}))}, 'R', 'r')).text.textContent;
        // Only a file input draws one.
        r.onNumber = toggleOf(renderSpec({json.dumps(dict(WEIGHT_FIELD, toggle={"key": "b", "label": "B"}))}, 'W', 'w'));
    """))

    # The picker, then the toggle - a checkbox in its label, unticked, its
    # text as text - then Save, all in the one row; the picker still the
    # field, its label over it, nothing as markup.
    assert r["row"] == [["input", "input"], ["label", "tg"], ["button", "btn sm"]]
    assert r["toggle"] == ["label", "input", "checkbox", False, "span", "Back<b>fill</b>"]
    assert r["look"]["field"] == ["input", "file", None, "Upload roster"]
    assert r["look"]["save"] == ["btn sm", "button", "Save", False]
    assert r["look"]["html"] == ""
    # A file input with no toggle: no checkbox - the row as before.
    assert r["plain"] == ["input", "button"]
    assert r["keyless"] is None
    assert r["unlabelled"] == "backfill"
    assert r["onNumber"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_toggle_unticked_sends_its_state_with_no_confirm(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = () => renderSpec({json.dumps(TOGGLED_FIELD)}, 'Roster', 'roster');
        r.unticked = await saveToggled(box(), [{{name:'roster.ics', size:16}}], false);
        // Ticked, but with no confirm: sent straight away too.
        const quiet = {json.dumps(dict(TOGGLED_FIELD, toggle={"key": "backfill", "label": "Backfill"}))};
        r.quiet = await saveToggled(renderSpec(quiet, 'Roster', 'roster'), [{{name:'roster.ics', size:16}}], true);
        r.modals = modals.length;
    """))

    fresh = {"type": "empty", "message": "fresh"}
    # No confirm asked; the file under the input's key, and the payload
    # the toggle's state under its own; the tile redrawn from the reply.
    assert r["unticked"] == {
        "asked": None, "held": [True, True, True], "sentWhileAsking": 1, "closed": None,
        "after": [False, False, False], "ticked": False, "msg": "",
        "sent": [["roster", "upload", "file", "roster.ics", {"for": "roster"}, {"backfill": False}]],
        "repaints": [["roster", fresh]],
    }
    assert r["quiet"]["asked"] is None
    assert r["quiet"]["sent"] == [["roster", "upload", "file", "roster.ics", {"for": "roster"}, {"backfill": True}]]
    assert r["modals"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_toggle_ticked_asks_its_confirm_and_sends_only_once_confirmed(client):
    page = _page(client)
    r = _node(_input_script(page, f"""
        const box = () => renderSpec({json.dumps(TOGGLED_FIELD)}, 'Roster', 'roster');
        r.confirmed = await saveToggled(box(), [{{name:'roster.ics', size:16}}], true, 'confirm');
        r.cancelled = await saveToggled(box(), [{{name:'roster.ics', size:16}}], true, 'cancel');
        r.dismissed = await saveToggled(box(), [{{name:'roster.ics', size:16}}], true, 'dismiss');
        // No file, or one over the cap: refused on the tile before any ask.
        r.none = await saveToggled(box(), [], true, 'confirm');
        r.big = await saveToggled(box(), [{{name:'huge.ics', size:UPLOAD_MAX + 1}}], true, 'confirm');
        // The app's refusal after a confirm: under the picker, still ticked.
        reply = {{ok:false, error:'No duties found in roster.ics'}};
        r.refused = await saveToggled(box(), [{{name:'roster.ics', size:16}}], true, 'confirm');
    """))

    fresh = {"type": "empty", "message": "fresh"}
    # The app's confirm, as text, under the toggle's label - held while it
    # asks, nothing sent until Continue; then {backfill: true} beside the
    # file, and the tile redrawn.
    confirmed = r["confirmed"]
    assert confirmed["asked"]["title"] == "Back<b>fill</b>"
    assert confirmed["asked"]["what"] == "Backfill <i>past</i> duties too?"
    assert "<b>" not in confirmed["asked"]["html"] and "<i>" not in confirmed["asked"]["html"]
    assert 'data-act="cancel"' in confirmed["asked"]["html"] and ">Continue</button>" in confirmed["asked"]["html"]
    assert (confirmed["held"], confirmed["sentWhileAsking"], confirmed["closed"]) == ([True, True, True], 0, True)
    assert confirmed["sent"] == [["roster", "upload", "file", "roster.ics", {"for": "roster"}, {"backfill": True}]]
    assert confirmed["repaints"] == [["roster", fresh]]
    assert confirmed["after"] == [False, False, False]
    # Cancelled, or closed any other way: nothing sent, the tile as it was
    # - still ticked, Save back.
    for case in ("cancelled", "dismissed"):
        out = r[case]
        assert out["asked"]["what"] == "Backfill <i>past</i> duties too?", case
        assert (out["sent"], out["repaints"], out["closed"], out["msg"]) == ([], [], True, ""), case
        assert (out["after"], out["ticked"]) == ([False, False, False], True), case
    # The tile's own refusals come first: no ask, nothing sent.
    assert (r["none"]["asked"], r["none"]["msg"], r["none"]["sent"]) == (None, "Choose a file.", [])
    assert (r["big"]["asked"], r["big"]["msg"], r["big"]["sent"]) == (None, "File too large (max 25 MB).", [])
    assert r["refused"]["sent"] == confirmed["sent"]
    assert (r["refused"]["msg"], r["refused"]["ticked"], r["refused"]["repaints"]) == ("No duties found in roster.ics", True, [])


def test_the_confirm_is_the_pages_own_modal_never_confirm(client):
    page = _page(client)
    ask = _js_function(page, "confirmFirst")
    # openModal's chrome - Escape and a click outside close it, and say
    # so - its words set as text; never the browser's confirm().
    assert "openModal(`" in ask and "() => done(go));" in ask
    assert "box.querySelector('[data-f=\"title\"]').textContent = title;" in ask
    assert "box.querySelector('[data-f=\"what\"]').textContent = text;" in ask
    assert "${" not in ask and "innerHTML" not in ask
    assert "confirm(" not in ask.replace("confirmFirst(", "")
    assert "      if(onClose) onClose();\n" in _js_function(page, "openModal")
    spec_input = _js_function(page, "specInput")
    assert "const payload = check ? {[tg.key]: check.checked} : undefined;" in spec_input
    assert "if(check && check.checked && tg.confirm!=null && tg.confirm!==''){" in spec_input
    assert "const go = await confirmFirst(tgLabel, String(tg.confirm));" in spec_input
    assert not re.search(r"(?<![\w.])confirm\(", spec_input)


def test_a_file_input_is_built_as_text_and_capped_as_the_server_caps_it(client):
    import main

    page = _page(client)
    spec_input = _js_function(page, "specInput")
    # The hints set as a property, never markup; the first file picked
    # sent by the file sender, the cap the server's own.
    assert "field.type = 'file';" in spec_input
    assert "if(s.accept!=null && s.accept!=='') field.accept = String(s.accept);" in spec_input
    assert "const file = field.files && field.files[0];" in spec_input
    assert "if(!file){ msg.textContent = 'Choose a file.'; return; }" in spec_input
    assert "if(file.size > UPLOAD_MAX){ msg.textContent = 'File too large (max 25 MB).'; return; }" in spec_input
    assert "api.appActionFile(appId, s.action, s.key, file, tileCfg(appId), payload);" in spec_input
    assert "if(!upload) field.onkeydown = e => { if(e.key==='Enter') submit(); };" in spec_input
    assert _const(page, "UPLOAD_MAX") == "const UPLOAD_MAX = 25 * 1024 * 1024;"
    assert 25 * 1024 * 1024 == main.MAX_UPLOAD_BYTES
    # The picker sits in the tile's row as the other fields do.
    assert '.spec-input input[type="file"].input{' in page
    assert '.spec-input input[type="file"].input::file-selector-button{' in page


# The page's own sender, run under node with fetch stood in for: what it
# hands fetch, as FormData - and, encoded by node's own Request, the
# bytes a browser would put on the wire.
FORM_SCRIPT = r"""
    const enc = encodeURIComponent;
    SENDER
    API
    const r = {}, sent = [];
    let answer = async () => ({ok:true, json:async () => ({ok:true, spec:{type:'empty', message:'fresh'}})});
    globalThis.fetch = async (url, opts) => { sent.push([url, opts]); return answer(); };
    const entry = ([k, v]) => [k, typeof v==='string' ? v : {name:v.name, type:v.type, size:v.size}];
    (async () => {
      const file = new File([CONTENT], 'weigh-ins.txt', {type:'text/plain'});
      r.reply = await api.appActionFile('import', 'import', 'file', file, {days:3});
      const [url, opts] = sent[0];
      r.url = url;
      r.request = {method:opts.method, headers:opts.headers===undefined ? null : opts.headers,
                   form:opts.body instanceof FormData, entries:[...opts.body.entries()].map(entry)};
      const wire = new Request('http://openfit.test' + url, {method:'POST', body:opts.body});
      r.contentType = wire.headers.get('content-type');
      r.body = Buffer.from(await wire.arrayBuffer()).toString('base64');
      // No cfg: an empty one. And the replies as send()'s are handed on.
      await api.appActionFile('a b', 'go', 'roster', file);
      r.bare = [sent[1][0], [...sent[1][1].body.entries()].map(entry)];
      answer = async () => ({ok:false, json:async () => ({ok:false, error:'Request too large (max 25 MB).'})});
      r.refused = await api.appActionFile('import', 'import', 'file', file, {});
      answer = async () => ({ok:false, json:async () => { throw new SyntaxError('not JSON'); }});
      r.unsaid = await api.appActionFile('import', 'import', 'file', file, {});
      answer = async () => { throw new TypeError('Failed to fetch'); };
      r.offline = await api.appActionFile('import', 'import', 'file', file, {});
      // A payload - a toggle's state - as a form field of its JSON.
      await api.appActionFile('roster', 'upload', 'file', file, {}, {backfill:true});
      r.withPayload = [...sent[sent.length-1][1].body.entries()].map(entry);
      console.log(JSON.stringify(r));
    })().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_file_input_posts_multipart_the_action_route_takes(client, tmp_path, monkeypatch):
    import base64
    import sqlite3
    import tempfile

    import main

    apps_dir, uploads = tmp_path / "apps", tmp_path / "uploads"
    apps_dir.mkdir()
    uploads.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(apps_dir))
    monkeypatch.setattr(tempfile, "tempdir", str(uploads))
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "apps", "import")
    files = {name: open(os.path.join(fixture, name), encoding="utf-8").read() for name in ("app.yaml", "app.py")}
    assert client.post("/api/apps", json={"files": files}).status_code == 201

    page = _page(client)
    content = "2026-01-03 81.2\n2026-01-05 80.9\n"
    script = (FORM_SCRIPT.replace("SENDER", _js(page, "sendForm")).replace("API", _js(page, "api"))
              .replace("CONTENT", json.dumps(content)))
    r = _node(script)

    # A POST to the action route of FormData - no Content-Type of the
    # page's own, so the browser sets the multipart one, boundary and all
    # - holding the action, the tile's config as JSON, and the file under
    # the input's key, as picked.
    assert r["url"] == "/api/app/import/action"
    assert r["request"] == {
        "method": "POST", "headers": None, "form": True,
        "entries": [["action", "import"], ["cfg", '{"days":3}'],
                    ["file", {"name": "weigh-ins.txt", "type": "text/plain", "size": len(content)}]],
    }
    assert r["reply"] == {"ok": True, "spec": {"type": "empty", "message": "fresh"}}
    assert r["bare"] == ["/api/app/a%20b/action", [
        ["action", "go"], ["cfg", "{}"], ["roster", {"name": "weigh-ins.txt", "type": "text/plain", "size": len(content)}],
    ]]
    # A refusal as send() hands one on: the server's reason, or none.
    assert r["refused"] == {"ok": False, "error": "Request too large (max 25 MB)."}
    assert r["unsaid"] == {"ok": False}
    assert r["offline"] == {"ok": False, "error": "network error"}
    # No payload given, no payload field (above); one given, its JSON as
    # one, before the file - the route parses it and puts the file in it.
    assert r["withPayload"] == [
        ["action", "upload"], ["cfg", "{}"], ["payload", '{"backfill":true}'],
        ["file", {"name": "weigh-ins.txt", "type": "text/plain", "size": len(content)}],
    ]

    # What a browser would send, sent: the route takes it, the app reads
    # the file, and its weigh-ins are filed on their days.
    assert r["contentType"].startswith("multipart/form-data; boundary=")
    resp = client.post("/api/app/import/action", data=base64.b64decode(r["body"]), content_type=r["contentType"])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["spec"]["children"][0]["value"] == 2
    assert os.listdir(uploads) == []
    conn = sqlite3.connect(main.app.config["DB_PATH"])
    assert conn.execute(
        "SELECT date, source, metric, value FROM metrics ORDER BY date"
    ).fetchall() == [("2026-01-03", "app:import", "weight_kg", 81.2), ("2026-01-05", "app:import", "weight_kg", 80.9)]
    conn.close()


def test_the_file_sender_reaches_the_action_route_through_the_api_block(client):
    page = _page(client)
    # Beside appAction, in the API block - the only place a URL lives.
    assert (
        "    appActionFile: (id, action, key, file, cfg, payload) => {\n"
        "      const form = new FormData();\n"
        "      form.append('action', action);\n"
        "      form.append('cfg', JSON.stringify(cfg || {}));\n"
        "      if(payload) form.append('payload', JSON.stringify(payload));\n"
        "      form.append(key, file);\n"
        "      return sendForm('/api/app/'+enc(id)+'/action', form);\n"
        "    },"
    ) in page
    send_form = _js(page, "sendForm")
    assert "fetch(url, {method:'POST', body:form})" in send_form
    assert "Content-Type" not in send_form
    assert page.count("sendForm(") == 2


# ---------- the timeline element ----------
#
# A timeline is one lane or two of time-blocks on one vertical time axis,
# laid out by the page to the viewport it scrolls in. The structure is
# the page's own renderer, run under node over the stand-in DOM. Its
# pixels aren't: the stand-in has no layout - every height is 0 - so the
# renderer falls back to TL_FALLBACK_PX, and only that the layout ran,
# and the floor a short block is held to, are checked here. The layout
# itself is a real browser's to check.

TIMELINE_PARTS = [
    "el", "SPECS", "renderSpec", "specMsg",
    "TL_VIEW_MS", "TL_MIN_WEEK_PX", "TL_MIN_BLOCK_PX", "TL_FALLBACK_PX", "TL_DAY_MS", "TL_MAX_DAY_LINES", "TL_MARK_MS",
    "TL_NOTCH_PX", "TL_GESTURE_MS", "timelines", "timelineSpec", "releaseTimelines",
]

TIMELINE_HARNESS = r"""
    // The frame after this one, and a resize: each noted, run when the
    // test says.
    const frames = [];
    const requestAnimationFrame = f => { frames.push(f); return frames.length; };
    const observers = [];
    class ResizeObserver {
      constructor(callback){ this.callback = callback; this.observed = []; this.disconnected = 0; observers.push(this); }
      observe(target){ this.observed.push(target); }
      disconnect(){ this.disconnected++; }
    }
    // What the other specs draw with, which these tests don't look at.
    const sparkline = () => mk('div'), specInput = () => mk('div'), fmtSpecValue = v => String(v);

    const kids = (n, cls) => n.children.filter(c => (c.className || '').split(/\s+/).includes(cls));
    const px = v => typeof v==='string' && /px$/.test(v) && Number.isFinite(parseFloat(v));
    // A timeline as drawn: its parts, its lanes' blocks and overlays, and
    // whether every position the layout sets was set.
    const look = box => {
      const scroll = box.querySelector('.tl-scroll');
      const track = scroll && kids(scroll, 'tl-track')[0];
      const lanes = track ? kids(track, 'tl-lane') : [];
      const head = box.querySelector('.tl-head');
      return {
        cls: box.className, kids: box.children.map(c => c.className),
        title: (kids(box, 't')[0] || {textContent:null}).textContent,
        head: head ? head.children.map(h => h.textContent) : null,
        track: track ? track.children.map(c => c.className) : null,
        lanes: lanes.map(l => ({cls:l.className, left:l.style.left, width:l.style.width,
          blocks: kids(l, 'tl-block').map(b => b.textContent),
          futures: kids(l, 'tl-future').length})),
        nows: track ? kids(track, 'tl-now').length : 0,
        bands: lanes.map(l => kids(l, 'tl-band').map(b => b.textContent)),
        gridlines: track ? kids(track, 'tl-grid').flatMap(g => kids(g, 'tl-gridline')).length : 0,
        laidOut: !!track && px(track.style.height)
          && kids(track, 'tl-grid').every(g => kids(g, 'tl-gridline').every(n => px(n.style.top)))
          && lanes.every(l => kids(l, 'tl-block').every(b => px(b.style.top) && px(b.style.height)))
          && kids(track, 'tl-now').every(n => px(n.style.top))
          && lanes.every(l => kids(l, 'tl-future').every(f => px(f.style.top)))
          && lanes.every(l => kids(l, 'tl-band').every(b => px(b.style.top) && px(b.style.height))),
        heights: lanes.map(l => kids(l, 'tl-block').map(b => parseFloat(b.style.height))),
        html: [box, ...box.querySelectorAll('*')].map(n => n._html).join(''),
        msg: (box.querySelector('.spec-msg') || {textContent:null}).textContent,
      };
    };
    const r = {};
"""


def _timeline_script(page, scenario):
    return (
        MINI_DOM + "\n".join(_js(page, n) for n in TIMELINE_PARTS) + TIMELINE_HARNESS
        + scenario + "\nconsole.log(JSON.stringify(r));"
    )


ROSTERED_LANE = {"label": "Rostered", "grey_after_now": False, "intervals": [
    {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T14:00:00Z", "label": "CX9101 <b>"},
    {"start": "2026-10-19T22:00:00Z", "end": "2026-10-22T10:00:00Z", "label": "R1E"},   # two and a half days
]}
FLOWN_LANE = {"label": "Flown", "grey_after_now": True, "intervals": [
    {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T14:00:00Z", "label": "CX9101"},
    {"start": "2026-10-12T01:00:00Z", "end": "2026-10-12T01:30:00Z", "label": "A1534"},  # half an hour
    {"start": "2026-10-14T01:00:00Z", "end": "2026-10-14T05:00:00Z", "label": None},
]}
TIMELINE = {"type": "timeline", "title": "Duties <i>", "now": "2026-10-18T12:00:00Z",
            "lanes": [ROSTERED_LANE, FLOWN_LANE]}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_timeline_draws_two_lanes_of_blocks_a_now_line_and_the_greyed_future(client):
    page = _page(client)
    r = _node(_timeline_script(page, f"""
        const box = renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster');
        r.built = look(box);
        r.observed = observers.map(o => o.observed.map(t => t.className));
        // The next frame lays it out again - at the fallback still, with
        // no height to measure here - and so does a resize.
        r.frames = frames.length;
        frames.splice(0).forEach(f => f());
        box.querySelector('.tl-scroll').isConnected = true;
        observers[0].callback([]);
        r.relaid = look(box);
        r.min = TL_MIN_BLOCK_PX;
        r.inPanel = renderSpec({{type:'panel', children:[{{type:'empty', message:'x'}}, {json.dumps(TIMELINE)}]}}, 'Roster', 'roster')
          .children.map(c => c.className);
    """))

    built = r["built"]
    # Its title, the lanes' headings over a scrolling frame; in the frame
    # the track - the day lines under everything, a lane each - lane 0 the
    # left half, lane 1 the right - and the now line across both.
    assert built["cls"] == "spec-timeline"
    assert built["kids"] == ["t", "tl-head", "tl-scroll"]
    assert built["title"] == "Duties <i>"
    assert built["head"] == ["Rostered", "Flown"]
    assert built["track"] == ["tl-grid", "tl-lane l0", "tl-lane l1", "tl-now"]
    assert built["gridlines"] == 12   # 11 to 22 October
    assert built["lanes"] == [
        {"cls": "tl-lane l0", "left": "0%", "width": "50%", "blocks": ["CX9101 <b>", "R1E"], "futures": 0},
        {"cls": "tl-lane l1", "left": "50%", "width": "50%", "blocks": ["CX9101", "A1534", ""], "futures": 1},
    ]
    assert built["nows"] == 1
    # Every one of an app's strings set as text, never markup.
    assert built["html"] == ""
    # Laid out at once, at the fallback height - no height to measure -
    # without a throw; a short block held to the floor, a long one not.
    assert built["laidOut"] is True
    [rostered, flown] = built["heights"]
    assert flown[1] == r["min"]
    assert rostered[1] > r["min"]
    # And again on the next frame, and on a resize, the observer watching
    # the frame.
    assert r["frames"] == 1
    assert r["observed"] == [["tl-scroll"]]
    assert r["relaid"] == built
    # In a panel, it's one of the children.
    assert r["inPanel"] == ["spec-msg", "spec-timeline"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_timeline_is_laid_out_to_its_viewport_and_opens_on_now(client):
    page = _page(client)
    r = _node(_timeline_script(page, f"""
        const box = renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster');
        const scroll = box.querySelector('.tl-scroll'), track = scroll.children[0];
        // The viewport given a height, as a browser would, and resized.
        const at = h => {{
          scroll.clientHeight = h;
          scroll.isConnected = true;
          observers[0].callback([]);
          return {{track: parseFloat(track.style.height), block: parseFloat(kids(track.children[1], 'tl-block')[0].style.height),
                  now: parseFloat(kids(track, 'tl-now')[0].style.top), top: scroll.scrollTop}};
        }};
        r.built = at(0);
        r.tall = at(480);
        r.hidden = at(0);
        r.short = at(62);
        r.fallback = TL_FALLBACK_PX;
    """))

    # Built, before it's on the page: laid out at the fallback, nothing
    # scrolled; hidden (no height), the last layout stands.
    assert "top" not in r["built"]   # undefined: never set
    assert r["hidden"] == r["tall"]
    # A week to the viewport's height: twice the height, twice the axis
    # and every block on it (this one long enough to clear the floor).
    assert r["tall"]["track"] == pytest.approx(r["built"]["track"] * 480 / r["fallback"])
    assert r["tall"]["block"] == pytest.approx(r["built"]["block"] * 480 / r["fallback"])
    # Its first real height opens it on now, mid-viewport.
    assert r["tall"]["now"] - r["tall"]["top"] == pytest.approx(240)
    # A viewport shorter than a week's least (a 2x1 tile's) keeps that
    # scale - less than a week in view, further to scroll - and the time
    # at its top through the resize.
    assert r["short"]["track"] == pytest.approx(r["built"]["track"])
    assert r["short"]["block"] == pytest.approx(r["built"]["block"])
    assert r["short"]["top"] / r["short"]["track"] == pytest.approx(r["tall"]["top"] / r["tall"]["track"])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_timeline_with_no_now_has_no_line_and_greys_nothing(client):
    page = _page(client)
    one = {"type": "timeline", "title": None, "now": None, "lanes": [FLOWN_LANE]}
    unlabelled = {"type": "timeline", "now": "2026-10-18T12:00:00Z",
                  "lanes": [{**ROSTERED_LANE, "label": None}, {**FLOWN_LANE, "label": ""}]}
    r = _node(_timeline_script(page, f"""
        r.one = look(renderSpec({json.dumps(one)}, 'Roster', 'roster'));
        r.unlabelled = look(renderSpec({json.dumps(unlabelled)}, 'Roster', 'roster'));
    """))

    # One lane, the whole width; no now, so no line - and nothing greyed,
    # grey_after_now or not, with no now to grey after.
    assert r["one"]["kids"] == ["tl-head", "tl-scroll"]
    assert r["one"]["track"] == ["tl-grid", "tl-lane l0"]
    assert r["one"]["lanes"] == [
        {"cls": "tl-lane l0", "left": "0%", "width": "100%", "blocks": ["CX9101", "A1534", ""], "futures": 0},
    ]
    assert r["one"]["nows"] == 0
    assert r["one"]["laidOut"] is True
    # No title, no headings: neither drawn.
    assert r["unlabelled"]["kids"] == ["tl-scroll"]
    assert r["unlabelled"]["nows"] == 1
    assert [lane["futures"] for lane in r["unlabelled"]["lanes"]] == [0, 1]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_hand_built_timeline_is_drawn_as_far_as_it_can_be(client):
    page = _page(client)
    good = {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T02:00:00Z", "label": "OK"}
    junk = {"type": "timeline", "now": "not a time", "lanes": [
        {"intervals": [good, {"start": "2026-10-10T03:00:00Z"}, {"start": "2026-10-10T05:00:00Z", "end": "2026-10-10T04:00:00Z"},
                       None, "CX105", {"start": 5, "end": 6}]},
        "a lane",
        {"intervals": [good]},
    ]}
    r = _node(_timeline_script(page, f"""
        r.junk = look(renderSpec({json.dumps(junk)}, 'X', 'x'));
        r.none = look(renderSpec({{type:'timeline', title:'Duties', lanes:[{{intervals:[]}}, {{intervals:[]}}]}}, 'X', 'x'));
        r.noLanes = look(renderSpec({{type:'timeline'}}, 'X', 'x'));
    """))

    # Two lanes at most; an interval without both ends, in order, left
    # out; a now that isn't a time, no line.
    assert [lane["blocks"] for lane in r["junk"]["lanes"]] == [["OK"], []]
    assert r["junk"]["nows"] == 0
    # Nothing to draw: said, under its title.
    assert r["none"]["kids"] == ["t", "spec-msg"]
    assert (r["none"]["title"], r["none"]["msg"]) == ("Duties", "No data")
    assert r["noLanes"]["msg"] == "No data"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_timeline_replaced_is_let_go_of(client):
    page = _page(client)
    r = _node(_timeline_script(page, f"""
        const a = renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster');
        const b = renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster');
        a.querySelector('.tl-scroll').isConnected = true;
        b.querySelector('.tl-scroll').isConnected = false;   // its tile drawn afresh
        r.before = timelines.size;
        releaseTimelines();
        r.after = timelines.size;
        r.disconnected = observers.map(o => o.disconnected);
        // A resize reaching one that's gone lets it go too.
        a.querySelector('.tl-scroll').isConnected = false;
        observers[0].callback([]);
        r.last = [timelines.size, observers.map(o => o.disconnected)];
    """))

    assert (r["before"], r["after"]) == (2, 1)
    assert r["disconnected"] == [0, 1]
    assert r["last"] == [0, [1, 1]]


def _ms(stamp):
    """A UTC timestamp as the page's Date.parse() reads it: ms since the epoch."""
    return datetime.fromisoformat(stamp).timestamp() * 1000


# A lane's bands: TIMELINE's, with trips behind the blocks - on the left
# one from the first block's start, its port written as markup to show
# it's set as text, and one starting before any block and ending past
# them all; on the right one out to now, unlabelled.
BANDED = {**TIMELINE, "lanes": [
    {**ROSTERED_LANE, "bands": [
        {"start": "2026-10-10T01:00:00Z", "end": "2026-10-12T00:00:00Z", "label": "ZZB <b>"},
        {"start": "2026-10-19T20:00:00Z", "end": "2026-10-23T00:00:00Z", "label": "ZZC"},
    ]},
    {**FLOWN_LANE, "bands": [{"start": "2026-10-10T01:00:00Z", "end": "2026-10-18T12:00:00Z", "label": None}]},
]}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_lanes_bands_are_shaded_behind_its_blocks(client):
    page = _page(client)
    r = _node(_timeline_script(page, f"""
        const box = renderSpec({json.dumps(BANDED)}, 'Roster', 'roster');
        r.built = look(box);
        // At a height, as a browser gives it: a week to 480px.
        const scroll = box.querySelector('.tl-scroll'), track = scroll.children[0];
        scroll.clientHeight = 480;
        scroll.isConnected = true;
        observers[0].callback([]);
        const lanes = kids(track, 'tl-lane');
        r.order = lanes.map(l => l.children.map(c => c.className));
        r.bands = lanes.map(l => kids(l, 'tl-band').map(b => ({{
          top: parseFloat(b.style.top), height: parseFloat(b.style.height),
          label: kids(b, 'tl-band-label').map(t => [t.textContent, parseFloat(t.style.top)])}})));
        r.blocks = lanes.map(l => kids(l, 'tl-block').map(b => [parseFloat(b.style.top), parseFloat(b.style.height)]));
        r.track = parseFloat(track.style.height);
        r.plain = look(renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster'));
    """))

    built = r["built"]
    # Each lane's bands in its own column, their labels as text; the
    # blocks, the now line and the grey as without them.
    assert built["bands"] == [["ZZB <b>", "ZZC"], [""]]
    assert built["lanes"] == r["plain"]["lanes"]
    assert built["track"] == ["tl-grid", "tl-lane l0", "tl-lane l1", "tl-now"]
    assert built["html"] == ""
    assert built["laidOut"] is True
    # First in each lane - so behind the grey and the blocks over them.
    assert r["order"] == [
        ["tl-band", "tl-band", "tl-block", "tl-block"],
        ["tl-band", "tl-future", "tl-block", "tl-block", "tl-block"],
    ]
    # On the blocks' scale - a week to the frame - from the axis' start,
    # the first block's: each band at its start, as long as it is (no
    # floor). Its label past the block over its top edge - the flight
    # out, which starts with it - or at its top with none there.
    week = 7 * 86400000 / 480
    at = lambda t: (_ms(t) - _ms("2026-10-10T01:00:00Z")) / week
    span = lambda a, b: (_ms(b) - _ms(a)) / week
    [cx9101, _] = r["blocks"][0]
    [[mel, zzc], [flown]] = r["bands"]
    assert mel == {"top": 0, "height": pytest.approx(span("2026-10-10T01:00:00Z", "2026-10-12T00:00:00Z")),
                   "label": [["ZZB <b>", pytest.approx(cx9101[1])]]}
    assert zzc == {"top": pytest.approx(at("2026-10-19T20:00:00Z")),
                   "height": pytest.approx(span("2026-10-19T20:00:00Z", "2026-10-23T00:00:00Z")), "label": [["ZZC", 0]]}
    assert flown == {"top": 0, "height": pytest.approx(span("2026-10-10T01:00:00Z", "2026-10-18T12:00:00Z")), "label": []}
    # The axis runs on to the end of the last band, past the last block.
    assert r["track"] == pytest.approx(at("2026-10-23T00:00:00Z"))
    # A lane without bands draws none.
    assert r["plain"]["bands"] == [[], []]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_hand_built_band_is_drawn_as_far_as_it_can_be(client):
    page = _page(client)
    good = {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T02:00:00Z", "label": "OK"}
    junk = {"type": "timeline", "lanes": [
        {"intervals": [good], "bands": [good, {"start": "2026-10-10T03:00:00Z"}, None, "MEL",
                                        {"start": "2026-10-10T05:00:00Z", "end": "2026-10-10T04:00:00Z"}]},
        {"intervals": [good], "bands": "MEL"},
    ]}
    r = _node(_timeline_script(page, f"""
        r.junk = look(renderSpec({json.dumps(junk)}, 'X', 'x'));
    """))

    # A band without both ends, in order, left out; bands not a list, none.
    assert r["junk"]["bands"] == [["OK"], []]
    assert r["junk"]["laidOut"] is True


def test_tiles_drawn_afresh_let_go_of_their_timelines(client):
    page = _page(client)
    # Wherever Home's faces are replaced - Home painted, or one tile redrawn.
    paint = _js_function(page, "paintHome")
    assert paint.endswith("\n    }\n    releaseTimelines();   // the faces just replaced")
    tile = _js_function(page, "repaintTile")
    assert tile.index("tile.replaceChildren(renderSpec(spec, t.name, appId));") < tile.index("releaseTimelines();")


def test_the_timeline_tones_are_in_every_theme(client):
    page = _page(client)
    css = page[: page.index("</style>")]
    light = css[css.index(":root{") : css.index("@media (prefers-color-scheme:dark)")]
    os_dark = css[css.index("@media (prefers-color-scheme:dark)") : css.index(':root[data-theme="dark"]{')]
    toggle_dark = css[css.index(':root[data-theme="dark"]{') :]
    toggle_dark = toggle_dark[: toggle_dark.index("}")]
    for block in (light, os_dark, toggle_dark):
        for token in ("--timeline-a:", "--timeline-a-ink:", "--timeline-b:", "--timeline-b-ink:"):
            assert token in block, token
    # Coloured by lane: lane 0 the lighter tone, lane 1 the darker.
    assert ".tl-lane.l0 .tl-block{background:var(--timeline-a);color:var(--timeline-a-ink);}" in css
    assert ".tl-lane.l1 .tl-block{background:var(--timeline-b);color:var(--timeline-b-ink);}" in css


def test_the_layover_tone_is_in_every_theme_and_tints_a_band(client):
    page = _page(client)
    css = page[: page.index("</style>")]
    light = css[css.index(":root{") : css.index("@media (prefers-color-scheme:dark)")]
    os_dark = css[css.index("@media (prefers-color-scheme:dark)") : css.index(':root[data-theme="dark"]{')]
    toggle_dark = css[css.index(':root[data-theme="dark"]{') :]
    toggle_dark = toggle_dark[: toggle_dark.index("}")]
    # Amber, its own token in every theme - so a theme can retune it.
    assert "--layover:#c07d18;" in light
    assert "--layover:#e0a83a;" in os_dark and "--layover:#e0a83a;" in toggle_dark
    # A soft tint of it, a dashed edge stronger; never in the way of a tap.
    band = css[css.index(".tl-band{") :].split("}")[0]
    assert "background:color-mix(in srgb,var(--layover) 14%,transparent)" in band
    assert "border-top:1px dashed" in band and "border-bottom:1px dashed" in band
    assert "pointer-events:none" in band


# ---------- a synced timeline: the focus clock ----------
#
# A timeline naming a focus group (spec.sync) is drawn around the
# group's clock: a fixed cursor over the middle of the frame, the time
# under it, and a Now button once it's been scrolled off now. Every
# timeline of one group scrolls to the same time - by its own scale -
# and a scroll by hand on one moves them all. Under node, over the
# stand-in DOM, with the clock held still and the follow tick's timer
# run when the test says.

SYNC_PARTS = TIMELINE_PARTS + [
    "TL_STEP_MS", "tlSnap", "focusGroups", "followTimer", "focusGroup", "setFocus", "followNow", "armFollow",
    "TL_DAYS", "TL_MONTHS", "fmtFocus",
]

SYNC_HARNESS = r"""
    // The clock, held at 12:07:30Z on Sunday 18 October 2026 - seven and
    // a half minutes after TIMELINE's now - until a test moves it on.
    let clock = Date.parse('2026-10-18T12:07:30Z');
    Date.now = () => clock;
    // The follow tick's timer: noted, run when the test says.
    const timers = [];
    const setTimeout = (f, ms) => { timers.push({f, ms}); return timers.length; };
    const WEEK = 7 * 86400000;
    // The viewport given a height and put on the page, as a browser
    // would, and laid out for it.
    const show = (box, h) => {
      const sc = box.querySelector('.tl-scroll');
      sc.clientHeight = h;
      sc.isConnected = true;
      observers.find(o => o.observed.includes(sc)).callback([]);
      return sc;
    };
    // Scrolled by hand: the browser moves it, then tells it.
    const scrollTo = (box, top) => { const sc = box.querySelector('.tl-scroll'); sc.scrollTop = top; sc.onscroll(); };
    // A synced timeline as drawn: its parts, the focus row and the frame,
    // and - laid out at height h, from its first block's start t0 - the
    // time under its cursor.
    const synced = (box, t0, h) => {
      const sc = box.querySelector('.tl-scroll');
      const focus = box.querySelector('.tl-focus');
      const view = box.querySelector('.tl-view');
      const btn = box.querySelector('.tl-nowbtn');
      const tag = box.querySelector('.tl-now-tag');
      const now = box.querySelector('.tl-now');
      const px = Math.max(h, TL_MIN_WEEK_PX) / WEEK;
      return {
        kids: box.children.map(c => c.className),
        focus: focus ? focus.children.map(c => c.className) : null,
        view: view ? view.children.map(c => c.className) : null,
        cursors: box.querySelectorAll('.tl-cursor').length,
        readout: (box.querySelector('.tl-readout') || {textContent: null}).textContent,
        nowBtn: btn ? {tag: btn.tag, text: btn.textContent, hidden: btn.hidden} : null,
        nowTag: tag ? {text: tag.textContent, left: tag.style.left, on: tag.parent.className} : null,
        top: sc.scrollTop,
        // The cursor is the viewport's middle: half a viewport into it.
        atCursor: sc.scrollTop===undefined ? null : new Date(t0 + sc.scrollTop / px).toISOString(),
        nowBelowCursor: now && sc.scrollTop!==undefined ? parseFloat(now.style.top) - (sc.scrollTop + h/2) : null,
      };
    };
"""


def _sync_script(page, scenario):
    return (
        MINI_DOM + "\n".join(_js(page, n) for n in SYNC_PARTS) + TIMELINE_HARNESS + SYNC_HARNESS
        + scenario + "\nconsole.log(JSON.stringify(r));"
    )


# TIMELINE's first block starts here: its axis' t0.
TIMELINE_T0 = "Date.parse('2026-10-10T01:00:00Z')"
SYNCED = {**TIMELINE, "sync": "frms"}
# Another member of the group: one lane, starting weeks earlier - so
# another axis, and another scroll position for the same time.
OTHER = {"type": "timeline", "title": "Sleep", "now": "2026-10-18T12:00:00Z", "sync": "frms", "lanes": [
    {"label": None, "grey_after_now": False, "intervals": [
        {"start": "2026-09-01T00:00:00Z", "end": "2026-09-01T07:00:00Z", "label": "Night"},
        {"start": "2026-10-17T23:00:00Z", "end": "2026-10-18T06:30:00Z", "label": "Night"},
    ]},
]}
OTHER_T0 = "Date.parse('2026-09-01T00:00:00Z')"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_synced_timeline_draws_a_cursor_a_readout_and_a_now_button(client):
    page = _page(client)
    r = _node(_sync_script(page, f"""
        // Unsynced - no sync, an empty name, not a name - first: none of
        // them starts a group, or the clock.
        r.plain = look(renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster'));
        r.plainKids = [{json.dumps(TIMELINE)}, {json.dumps({**TIMELINE, "sync": ""})}, {json.dumps({**TIMELINE, "sync": 5})}]
          .map(spec => renderSpec(spec, 'Roster', 'roster'))
          .map(box => [box.children.map(c => c.className),
                       ['.tl-focus', '.tl-readout', '.tl-nowbtn', '.tl-view', '.tl-cursor', '.tl-now-tag']
                         .map(sel => box.querySelectorAll(sel).length)]);
        r.groupsUnsynced = [focusGroups.size, timers.length];
        // Synced, with nothing to draw: said, as unsynced - no group.
        r.none = look(renderSpec({{type:'timeline', sync:'frms', lanes:[{{intervals:[]}}]}}, 'X', 'x'));
        r.groupsNone = focusGroups.size;

        const box = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        r.built = look(box);
        r.drawn = synced(box, {TIMELINE_T0}, 480);
        show(box, 480);
        r.shown = synced(box, {TIMELINE_T0}, 480);
        r.group = [...focusGroups].map(([name, g]) => [name, new Date(g.focusMs).toISOString(), g.following, g.members.size]);
        r.timers = timers.map(t => t.ms);
        // The readout's form, at another time: the quarter hour it's in.
        r.form = fmtFocus(tlSnap(Date.parse('2025-11-03T14:22:59Z')));
    """))

    # Unsynced, exactly as before: the title, the headings, the frame -
    # no focus row, no cursor, no Now tag - and no group, no clock.
    assert r["plain"]["kids"] == ["t", "tl-head", "tl-scroll"]
    assert r["plain"]["track"] == ["tl-grid", "tl-lane l0", "tl-lane l1", "tl-now"]
    assert r["plainKids"] == [[["t", "tl-head", "tl-scroll"], [0, 0, 0, 0, 0, 0]]] * 3
    assert r["groupsUnsynced"] == [0, 0]
    assert (r["none"]["kids"], r["none"]["msg"]) == (["spec-msg"], "No data")
    assert r["groupsNone"] == 0

    # Synced: the focus row - the readout, the Now button - over the
    # headings, and the frame wrapped with the cursor over it; inside it
    # the same track: the lanes, their blocks, the now line.
    built, drawn = r["built"], r["drawn"]
    assert built["kids"] == ["t", "tl-focus", "tl-head", "tl-view"]
    assert drawn["focus"] == ["tl-readout", "btn sm tl-nowbtn"]
    assert drawn["view"] == ["tl-scroll", "tl-cursor"]
    assert drawn["cursors"] == 1
    assert built["track"] == ["tl-grid", "tl-lane l0", "tl-lane l1", "tl-now"]
    assert [lane["blocks"] for lane in built["lanes"]] == [["CX9101 <b>", "R1E"], ["CX9101", "A1534", ""]]
    assert built["laidOut"] is True
    assert built["html"] == ""
    # The now line tagged Now, on the greyed lane - the right half.
    assert drawn["nowTag"] == {"text": "Now", "left": "calc(50% + 4px)", "on": "tl-now"}
    # The readout: the group's time, UTC, to the quarter hour - now, as
    # it's following; so no Now button.
    assert drawn["readout"] == "Sun 18 Oct 12:00Z"
    assert drawn["nowBtn"] == {"tag": "button", "text": "Now", "hidden": True}
    assert r["form"] == "Mon 03 Nov 14:15Z"
    # The group: started on now, following, the timeline its member; and
    # the follow tick due on the next quarter hour.
    assert r["group"] == [["frms", "2026-10-18T12:00:00.000Z", True, 1]]
    assert r["timers"] == [7.5 * 60000]
    # Built off the page, nothing's scrolled; given a height, the
    # group's time sits under the cursor - and while it follows, the now
    # line with it.
    assert drawn["atCursor"] is None
    assert r["shown"]["atCursor"] == "2026-10-18T12:00:00.000Z"
    assert r["shown"]["nowBelowCursor"] == pytest.approx(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_synced_timeline_with_bands_keeps_its_cursor_readout_and_now(client):
    page = _page(client)
    r = _node(_sync_script(page, f"""
        const plain = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        const banded = renderSpec({json.dumps({**BANDED, "sync": "frms"})}, 'Roster', 'roster');
        show(plain, 480); show(banded, 480);
        // The same time under both cursors - the same axis start - and
        // the same focus row, frame and cursor; only the bands added.
        r.plain = synced(plain, {TIMELINE_T0}, 480);
        r.banded = synced(banded, {TIMELINE_T0}, 480);
        r.bands = look(banded).bands;
        r.laidOut = look(banded).laidOut;
        // Padded half a frame for the cursor, as the blocks are.
        const sc = banded.querySelector('.tl-scroll');
        r.first = [parseFloat(kids(kids(sc.children[0], 'tl-lane')[0], 'tl-band')[0].style.top),
                   parseFloat(kids(kids(sc.children[0], 'tl-lane')[0], 'tl-block')[0].style.top)];
    """))

    assert r["banded"] == r["plain"]
    assert r["banded"]["kids"] == ["t", "tl-focus", "tl-head", "tl-view"]
    assert r["banded"]["view"] == ["tl-scroll", "tl-cursor"]
    assert r["banded"]["readout"] == "Sun 18 Oct 12:00Z"
    assert r["bands"] == [["ZZB <b>", "ZZC"], [""]]
    assert r["laidOut"] is True
    assert r["first"] == [240, 240]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_timelines_of_one_focus_group_scroll_together_by_time(client):
    page = _page(client)
    other_group = {**OTHER, "sync": "elsewhere"}
    r = _node(_sync_script(page, f"""
        const a = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        const b = renderSpec({json.dumps(OTHER)}, 'Sleep', 'sleep');
        const c = renderSpec({json.dumps(other_group)}, 'Sleep', 'sleep');
        // Two scales: a tall frame and a short one.
        show(a, 480); show(b, 300); show(c, 300);
        const look3 = () => [synced(a, {TIMELINE_T0}, 480), synced(b, {OTHER_T0}, 300), synced(c, {OTHER_T0}, 300)];
        r.opened = look3();
        // Its own scroll - the browser telling it of the scroll it made to
        // the focus - isn't the user's: still following.
        a.querySelector('.tl-scroll').onscroll();
        r.ownScroll = [focusGroups.get('frms').following, look3()];
        // a scrolled by hand to 05:40Z on Monday 12 October (from its t0,
        // at its scale): the group's focus moves to the quarter hour
        // under its cursor, and b - another axis, another scale - follows.
        const aTop = (Date.parse('2026-10-12T05:40:00Z') - {TIMELINE_T0}) * 480 / WEEK;
        scrollTo(a, aTop);
        r.scrolled = look3();
        r.aTop = aTop;
        r.group = [new Date(focusGroups.get('frms').focusMs).toISOString(), focusGroups.get('frms').following];
        // b's own scroll to it, told: passes.
        b.querySelector('.tl-scroll').onscroll();
        r.bEcho = new Date(focusGroups.get('frms').focusMs).toISOString();
        // Now b scrolled by hand, back a day: a follows.
        scrollTo(b, b.querySelector('.tl-scroll').scrollTop - 86400000 * 300 / WEEK);
        r.back = look3();
        // A member drawn afresh - its tile redrawn, the new face on the
        // page, the old one off it - leaves; the new one joins and opens
        // on the group's time, not now.
        const a2 = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        show(a2, 480);
        a.querySelector('.tl-scroll').isConnected = false;
        releaseTimelines();
        r.redrawn = [synced(a2, {TIMELINE_T0}, 480), focusGroups.get('frms').members.size, focusGroups.size];
    """))

    [a, b, c] = r["opened"]
    # Opened together on now - each its own axis and scale, so its own
    # scroll position, the same time.
    assert a["atCursor"] == b["atCursor"] == c["atCursor"] == "2026-10-18T12:00:00.000Z"
    assert a["top"] != pytest.approx(b["top"])
    assert r["ownScroll"] == [True, r["opened"]]

    [a, b, c] = r["scrolled"]
    # a stays where it was put - no snapping back under the user's hand -
    # and says the quarter hour it's in; b is scrolled to that time by
    # its own scale, and says the same. Both offer Now.
    assert a["top"] == pytest.approx(r["aTop"])
    assert a["readout"] == b["readout"] == "Mon 12 Oct 05:30Z"
    assert b["atCursor"] == "2026-10-12T05:30:00.000Z"
    assert b["top"] != pytest.approx(a["top"])
    assert a["nowBtn"]["hidden"] is False and b["nowBtn"]["hidden"] is False
    # The group's focus: that quarter hour, no longer following now.
    assert r["group"] == ["2026-10-12T05:30:00.000Z", False]
    # Another group's timeline isn't moved: still on now, following.
    assert c == r["opened"][2]
    assert r["bEcho"] == "2026-10-12T05:30:00.000Z"

    # b scrolled back a day by hand: a follows it there.
    [a, b, c] = r["back"]
    assert b["readout"] == a["readout"] == "Sun 11 Oct 05:30Z"
    assert a["atCursor"] == "2026-10-11T05:30:00.000Z"
    assert c == r["opened"][2]

    # A tile drawn afresh: the old member gone, the new one in, on the
    # group's time - which outlives its timelines.
    [a2, members, groups] = r["redrawn"]
    assert a2["atCursor"] == "2026-10-11T05:30:00.000Z"
    assert a2["readout"] == "Sun 11 Oct 05:30Z"
    assert a2["nowBtn"]["hidden"] is False
    assert (members, groups) == (2, 2)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_now_button_shows_once_scrolled_off_now_and_brings_the_group_back(client):
    page = _page(client)
    r = _node(_sync_script(page, f"""
        const a = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        const b = renderSpec({json.dumps(OTHER)}, 'Sleep', 'sleep');
        show(a, 480); show(b, 300);
        const hidden = () => [a, b].map(x => x.querySelector('.tl-nowbtn').hidden);
        r.following = hidden();
        // The follow tick, on the quarter hour, twenty minutes on: the
        // group moves on to now, and every member with it.
        clock = Date.parse('2026-10-18T12:27:30Z');
        timers[0].f();
        r.ticked = [synced(a, {TIMELINE_T0}, 480).atCursor, synced(b, {OTHER_T0}, 300).readout, hidden(),
                    timers.map(t => t.ms)];
        // Scrolled by hand: off now, so Now on every member.
        scrollTo(a, (Date.parse('2026-10-14T03:00:00Z') - {TIMELINE_T0}) * 480 / WEEK);
        r.scrolled = [hidden(), synced(b, {OTHER_T0}, 300).readout];
        // The tick doesn't move a group that isn't following.
        clock = Date.parse('2026-10-18T12:45:00Z');
        timers[1].f();
        r.held = [synced(b, {OTHER_T0}, 300).readout, hidden()];
        // Now, on b: the group back on now, following - every member
        // scrolled there, Now hidden again.
        b.querySelector('.tl-nowbtn').onclick();
        r.back = [focusGroups.get('frms').following, synced(a, {TIMELINE_T0}, 480).atCursor,
                  synced(b, {OTHER_T0}, 300).atCursor, synced(a, {TIMELINE_T0}, 480).readout, hidden()];
    """))

    # Following: no Now button on any member.
    assert r["following"] == [True, True]
    # The tick: on to 12:15Z everywhere, still following; due again in
    # fifteen minutes - the next quarter hour.
    assert r["ticked"] == ["2026-10-18T12:15:00.000Z", "Sun 18 Oct 12:15Z", [True, True], [7.5 * 60000, 2.5 * 60000]]
    # Scrolled off now: Now shown on both.
    assert r["scrolled"] == [[False, False], "Wed 14 Oct 03:00Z"]
    assert r["held"] == ["Wed 14 Oct 03:00Z", [False, False]]
    # Now: back on now - the quarter hour - following again, Now hidden.
    assert r["back"] == [True, "2026-10-18T12:45:00.000Z", "2026-10-18T12:45:00.000Z", "Sun 18 Oct 12:45Z", [True, True]]


def test_a_synced_timelines_cursor_and_now_line_are_the_accent(client):
    page = _page(client)
    css = page[: page.index("</style>")]
    # Two cyan lines: the fixed cursor and the now line it meets while following.
    assert "border-top:2px solid var(--accent)" in css[css.index(".tl-cursor{") :].split("}")[0]
    assert "border-top:2px solid var(--accent)" in css[css.index(".tl-now{") :].split("}")[0]
    assert "background:var(--accent);color:var(--accent-ink)" in css[css.index(".tl-now-tag{") :].split("}")[0]
    # The track scrolls under the cursor: it's over the frame, not in it.
    assert ".tl-view{position:relative;" in css


# ---------- rails: Plan and Report beside a synced timeline ----------
#
# A rails frame draws its timeline exactly as it's drawn alone, with a
# rail of fixed-size cards either side - each card four corners: the
# flight out and the port over the flight home and the day it left. A
# rail shows at most five of its cards, stacked from its top, and which
# by its side: a plan rail looks ahead of the focus time - the first
# card not over and on, its top card the head - and a report rail back -
# the latest card over at its foot, the head - each held full at the
# list's ends. It restacks whenever its focus group's time moves, which
# it only ever reads. Under node, over the stand-in DOM, with the clock
# held still as for the focus clock.

RAILS_PARTS = SYNC_PARTS + ["RAIL_MAX", "RAIL_CARD_PX", "RAIL_GAP_PX", "railWindow", "fmtCardDay", "railSpec", "railsSpec"]

RAILS_HARNESS = r"""
    // A rail as drawn: its rows, its heading, and its cards - each its
    // corners in reading order (out, port, in, day); > for the head.
    const rail = box => {
      const list = box.querySelector('.rail-cards');
      return {
        rows: box.children.map(c => c.className),
        title: box.querySelector('.rail-head').textContent,
        none: (list.querySelector('.rail-none') || {textContent: null}).textContent,
        cards: kids(list, 'rail-card').map(c => (c.classList.contains('head') ? '>' : '')
          + c.children.map(x => x.textContent).join(' ')),
      };
    };
    // The rail's cards given a height and put on the page, and laid out for it.
    const showRail = (box, h) => {
      const list = box.querySelector('.rail-cards');
      list.clientHeight = h;
      list.isConnected = true;
      observers.find(o => o.observed.includes(list)).callback([]);
    };
    // A node and everything under it, as drawn: to tell two drawings apart.
    const tree = n => [n.tag, n.className, n._text, n._html, JSON.stringify(n.style), JSON.stringify(n.attrs), n.children.map(tree)];
"""


def _rails_script(page, scenario):
    return (
        MINI_DOM + "\n".join(_js(page, n) for n in RAILS_PARTS) + TIMELINE_HARNESS + SYNC_HARNESS + RAILS_HARNESS
        + scenario + "\nconsole.log(JSON.stringify(r));"
    )


def _trip(start, end, port, out, inbound):
    return {"id": start, "start": start, "end": end, "out": out, "in": inbound, "port": port}


# Seven trips to plan - the first under way at the clock's now (12:00Z,
# Sunday 18 October 2026), the last not home yet - given out of order,
# and seven to report, all home.
PLAN = {"type": "rail", "side": "plan", "title": "Plan", "sync": "frms", "cards": [
    _trip("2026-10-21T01:00:00Z", "2026-10-23T13:00:00Z", "YVR", "CX888", "CX889"),
    _trip("2026-10-17T01:00:00Z", "2026-10-19T13:00:00Z", "BLR", "CX601", "CX602"),
    _trip("2026-10-25T01:00:00Z", "2026-10-27T13:00:00Z", "DEL", "CX695", "CX694"),
    _trip("2026-10-29T01:00:00Z", "2026-10-31T13:00:00Z", "JFK", "CX840", "CX841"),
    _trip("2026-11-02T01:00:00Z", "2026-11-02T09:00:00Z", "MNL", "CX903", "CX902"),
    _trip("2026-11-05T01:00:00Z", "2026-11-08T13:00:00Z", "CDG", "CX261", "CX260"),
    _trip("2026-11-12T01:00:00Z", None, "ZRH", "CX383", None),
]}
REPORT = {"type": "rail", "side": "report", "title": "Report", "sync": "frms", "cards": [
    _trip("2026-08-28T01:00:00Z", "2026-08-30T13:00:00Z", "HND", "CX548", "CX549"),
    _trip("2026-09-10T01:00:00Z", "2026-09-12T13:00:00Z", "MEL", "CX105", "CX104"),
    _trip("2026-09-20T01:00:00Z", "2026-09-22T13:00:00Z", "BKK", "CX701", "CX700"),
    _trip("2026-09-28T01:00:00Z", "2026-09-30T13:00:00Z", "LHR", "CX255", "CX254"),
    _trip("2026-10-03T01:00:00Z", "2026-10-05T13:00:00Z", "SIN", "CX711", "CX710"),
    _trip("2026-10-08T01:00:00Z", "2026-10-10T13:00:00Z", "NRT", "CX500", "CX501"),
    _trip("2026-10-12T01:00:00Z", "2026-10-14T13:00:00Z", "SYD", "CX101", "CX100"),
]}
RAILS = {"type": "rails", "left": PLAN, "timeline": SYNCED, "right": REPORT}

# Each card's corners as drawn: out, port, in - a dash for none - and the
# day it left.
PLAN_CARDS = {
    "BLR": "CX601 BLR CX602 17 Oct", "YVR": "CX888 YVR CX889 21 Oct", "DEL": "CX695 DEL CX694 25 Oct",
    "JFK": "CX840 JFK CX841 29 Oct", "MNL": "CX903 MNL CX902 02 Nov", "CDG": "CX261 CDG CX260 05 Nov",
    "ZRH": "CX383 ZRH — 12 Nov",
}
REPORT_CARDS = {
    "HND": "CX548 HND CX549 28 Aug", "MEL": "CX105 MEL CX104 10 Sep", "BKK": "CX701 BKK CX700 20 Sep",
    "LHR": "CX255 LHR CX254 28 Sep", "SIN": "CX711 SIN CX710 03 Oct", "NRT": "CX500 NRT CX501 08 Oct",
    "SYD": "CX101 SYD CX100 12 Oct",
}


def _cards(table, ports, head=None):
    return [(">" if port == head else "") + table[port] for port in ports]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_rails_window_looks_ahead_on_plan_and_back_on_report(client):
    page = _page(client)
    r = _node(_rails_script(page, """
        // Seven cards of ten ms each, twenty apart; the last open.
        const cards = [0, 20, 40, 60, 80, 100, 120].map(a => ({a, b: a===120 ? null : a + 10}));
        const at = (side, focus, fit, list) => railWindow(list || cards, focus, fit, side);
        const both = (focus, fit, list) => [at('plan', focus, fit, list), at('report', focus, fit, list)];
        r.before = both(-5, 5);       // before them all
        r.inOne = both(25, 5);        // in the second, the first over
        r.atItsEnd = both(30, 5);     // the second just over
        r.mid = both(95, 5);          // the fifth over, in none
        r.inSixth = both(105, 5);
        r.inOpen = both(500, 5);      // long after, in the open one
        r.short = [both(500, 3), both(55, 3), both(75, 3)];
        r.none = both(25, 0);         // room for none
        r.capped = both(500, 9);      // room for nine: never more than RAIL_MAX
        r.few = both(500, 5, cards.slice(0, 2));
        r.empty = both(25, 5, []);
        r.otherSide = at('sideways', 25, 5);   // anything but report looks ahead
        r.max = RAIL_MAX;
    """))

    w = lambda start, count, head: {"start": start, "count": count, "head": head}
    assert r["max"] == 5
    # Plan: from the first not over, held at the list's end, the head its
    # top card. Report: up to the latest over, held full at the list's
    # start, the head its bottom card - the open card never over.
    assert r["before"] == [w(0, 5, 0), w(0, 5, 4)]
    assert r["inOne"] == [w(1, 5, 0), w(0, 5, 4)]
    assert r["atItsEnd"] == [w(2, 5, 0), w(0, 5, 4)]
    assert r["mid"] == [w(2, 5, 0), w(0, 5, 4)]
    assert r["inSixth"] == [w(2, 5, 0), w(0, 5, 4)]
    assert r["inOpen"] == [w(2, 5, 0), w(1, 5, 4)]
    assert r["short"] == [[w(4, 3, 0), w(3, 3, 2)], [w(3, 3, 0), w(0, 3, 2)], [w(4, 3, 0), w(1, 3, 2)]]
    assert r["none"] == [w(0, 0, -1), w(0, 0, -1)]
    assert r["capped"] == [w(2, 5, 0), w(1, 5, 4)]
    assert r["few"] == [w(0, 2, 0), w(0, 2, 1)]
    assert r["empty"] == [w(0, 0, -1), w(0, 0, -1)]
    assert r["otherSide"] == w(1, 5, 0)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_rails_frame_draws_its_timeline_as_it_is_alone_between_two_rails(client):
    page = _page(client)
    r = _node(_rails_script(page, f"""
        const alone = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        const box = renderSpec({json.dumps(RAILS)}, 'Roster', 'roster');
        r.cls = box.className;
        r.kids = box.children.map(c => c.className);
        // The middle, the timeline: drawn as it is on its own, node for
        // node - bar its Now button, moved out of its focus row.
        alone.querySelector('.tl-nowbtn').remove();
        r.same = JSON.stringify(tree(box.children[1])) === JSON.stringify(tree(alone));
        r.centre = look(box.children[1]);
        r.left = rail(box.children[0]);
        r.right = rail(box.children[2]);
        // Members of the timeline's group, beside it - which they read,
        // and never move: still on now, following.
        const g = focusGroups.get('frms');
        r.group = [new Date(g.focusMs).toISOString(), g.following, g.members.size, timelines.size, focusGroups.size];
        const list = box.querySelector('.rail-cards'), card = box.querySelector('.rail-card');
        r.list = [list.attrs.role, list.attrs['aria-label']];
        r.card = [card.attrs.role, card.attrs['aria-current'], card.dataset.id, card.children.map(c => c.className)];
        r.heads = box.querySelectorAll('.rail-card.head').map(c => [c.dataset.id, c.attrs['aria-current']]);
        r.current = box.querySelectorAll('.rail-card').filter(c => c.attrs['aria-current']).length;
        // A card's size, and the gap between two, the CSS's: none set here.
        r.inline = [list, ...box.querySelectorAll('.rail-card')].filter(n => 'height' in n.style || 'gap' in n.style).length;
        r.counts = box.querySelectorAll('.rail-count').length;
        r.html = [box, ...box.querySelectorAll('*')].map(n => n._html).join('');
    """))

    # Four columns: a rail, the timeline's two lanes, a rail - and the
    # timeline's Now button, the frame's.
    assert r["cls"] == "spec-rails"
    assert r["kids"] == ["spec-rail", "spec-timeline", "spec-rail", "rails-now"]
    assert r["same"] is True
    assert r["centre"]["kids"] == ["t", "tl-focus", "tl-head", "tl-view"]
    assert r["centre"]["laidOut"] is True
    # Each rail mirrors the rows over the timeline's frame - its title's
    # line, its focus row - so its heading is in the lanes' heading row
    # and its cards start level with the frame; the heading its title
    # alone, no count.
    assert r["left"]["rows"] == r["right"]["rows"] == ["rail-sp t", "rail-sp", "rail-head", "rail-cards"]
    assert (r["left"]["title"], r["right"]["title"], r["counts"]) == ("Plan", "Report", 0)
    # Five at most, stacked from the top, in time order. Plan from the
    # trip under way on, the head its top card; Report up to the latest
    # home, the head its bottom card, older trips above it.
    assert r["left"]["cards"] == _cards(PLAN_CARDS, ["BLR", "YVR", "DEL", "JFK", "MNL"], head="BLR")
    assert r["right"]["cards"] == _cards(REPORT_CARDS, ["BKK", "LHR", "SIN", "NRT", "SYD"], head="SYD")
    assert r["heads"] == [["2026-10-17T01:00:00Z", "true"], ["2026-10-12T01:00:00Z", "true"]]
    assert r["current"] == 2
    assert r["group"] == ["2026-10-18T12:00:00.000Z", True, 4, 4, 1]
    assert r["list"] == ["list", "Plan: 7 cards"]
    # A card's four corners in reading order, its id carried; its size -
    # roomy, or compact in a narrow rail - and the gap the CSS's, so
    # nothing inline.
    assert r["card"] == ["listitem", "true", "2026-10-17T01:00:00Z", ["rc-out", "rc-port", "rc-in", "rc-day"]]
    assert r["inline"] == 0
    # Everything an app sent set as text.
    assert r["html"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_rails_restack_as_the_focus_moves_and_never_move_it(client):
    page = _page(client)
    r = _node(_rails_script(page, f"""
        const box = renderSpec({json.dumps(RAILS)}, 'Roster', 'roster');
        const [L, tl, R] = box.children;
        show(tl, 480);
        showRail(L, 424); showRail(R, 424);
        const both = () => [rail(L).cards, rail(R).cards];
        const g = focusGroups.get('frms');
        r.opened = [both(), new Date(g.focusMs).toISOString(), g.following];
        const to = iso => scrollTo(tl, (Date.parse(iso) - {TIMELINE_T0}) * 480 / WEEK);
        // The timeline scrolled by hand, ahead: the plan restacks - the
        // trips over by then off the top, the window held at its end; the
        // reports, looking back, stay on the latest five home.
        to('2026-10-26T05:40:00Z');
        r.ahead = both();
        // Back, into SYD's trip: NRT the latest home, at the foot - SYD,
        // not home yet then, off it - and MEL come in at the top.
        to('2026-10-12T06:00:00Z');
        r.back = both();
        // Further, into LHR's trip: held full at the list's start.
        to('2026-09-29T00:00:00Z');
        r.further = both();
        // Now: every member back on now, the rails with them.
        box.querySelector('.tl-nowbtn').onclick();
        r.now = both();
        // The follow tick, once the trip under way is home: off the plan.
        clock = Date.parse('2026-10-19T13:07:30Z');
        timers[timers.length - 1].f();
        r.ticked = [both(), new Date(g.focusMs).toISOString(), g.following];
    """))

    [plan, report], focus, following = r["opened"]
    assert (focus, following) == ("2026-10-18T12:00:00.000Z", True)
    assert plan == _cards(PLAN_CARDS, ["BLR", "YVR", "DEL", "JFK", "MNL"], head="BLR")
    assert report == _cards(REPORT_CARDS, ["BKK", "LHR", "SIN", "NRT", "SYD"], head="SYD")

    [plan, report] = r["ahead"]
    assert plan == _cards(PLAN_CARDS, ["DEL", "JFK", "MNL", "CDG", "ZRH"], head="DEL")
    assert report == r["opened"][0][1]

    [plan, report] = r["back"]
    assert plan == r["opened"][0][0]
    assert report == _cards(REPORT_CARDS, ["MEL", "BKK", "LHR", "SIN", "NRT"], head="NRT")
    [plan, report] = r["further"]
    assert report == _cards(REPORT_CARDS, ["HND", "MEL", "BKK", "LHR", "SIN"], head="SIN")

    assert r["now"] == r["opened"][0]

    [plan, report], focus, following = r["ticked"]
    assert (focus, following) == ("2026-10-19T13:00:00.000Z", True)
    assert plan == _cards(PLAN_CARDS, ["YVR", "DEL", "JFK", "MNL", "CDG"], head="YVR")
    assert report == r["opened"][0][1]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_rail_shows_as_many_whole_cards_as_fit(client):
    page = _page(client)
    r = _node(_rails_script(page, f"""
        const box = renderSpec({json.dumps(RAILS)}, 'Roster', 'roster');
        const [L, tl, R] = box.children;
        show(tl, 480);
        // Built off the page: drawn for five, until it's measured.
        r.built = [rail(L).cards.length, rail(R).cards.length];
        const three = 3 * RAIL_CARD_PX + 2 * RAIL_GAP_PX;
        const at = h => {{ showRail(L, h); return rail(L).cards; }};
        r.three = at(three);
        r.two = at(three - 1);
        // No height - Home behind another tab - and the last layout stands.
        r.hidden = at(0);
        r.tall = at(2000).length;
        // Three on each side: Plan from the trip under way, Report the
        // latest three home - the head at its foot.
        showRail(L, three); showRail(R, three);
        r.shortReport = rail(R).cards;
        // At the plan's end: held, the last three, the head its top.
        scrollTo(tl, (Date.parse('2026-11-20T00:00:00Z') - {TIMELINE_T0}) * 480 / WEEK);
        r.end = rail(L).cards;
        r.frames = frames.length;
    """))

    assert r["built"] == [5, 5]
    assert r["three"] == _cards(PLAN_CARDS, ["BLR", "YVR", "DEL"], head="BLR")
    assert r["two"] == _cards(PLAN_CARDS, ["BLR", "YVR"], head="BLR")
    assert r["hidden"] == r["two"]
    assert r["tall"] == 5
    assert r["shortReport"] == _cards(REPORT_CARDS, ["SIN", "NRT", "SYD"], head="SYD")
    assert r["end"] == _cards(PLAN_CARDS, ["MNL", "CDG", "ZRH"], head="MNL")
    # Measured again on the next frame, as a timeline is.
    assert r["frames"] >= 3


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_rail_counts_whole_cards_of_the_size_the_css_gives_them(client):
    page = _page(client)
    r = _node(_rails_script(page, f"""
        const box = renderSpec({json.dumps(RAILS)}, 'Roster', 'roster');
        const [L, tl, R] = box.children;
        show(tl, 480);
        // A card's height is the CSS's - the roomy 58px, or the compact
        // 46px in a narrow rail - read off a card on the page: here, the
        // cards drawn are given it, as a browser would lay them out.
        const sized = (side, px) => side.querySelectorAll('.rail-card').forEach(c => {{ c.offsetHeight = px; }});
        const at = (side, px, h) => {{ sized(side, px); showRail(side, h); return rail(side).cards; }};
        const four = 4 * 46 + 3 * RAIL_GAP_PX;
        // Compact: four whole cards in a column four tall, three a pixel short.
        r.compact = [at(L, 46, four), at(L, 46, four - 1), at(R, 46, four)];
        // A 2x2's column, as Chromium lays it out (231.6px): four a side.
        r.twoByTwo = [at(L, 46, 231), at(R, 46, 231)].map(c => c.length);
        // The cards drawn afresh aren't measured yet: the last measured stands.
        showRail(L, four);
        r.redrawn = rail(L).cards;
        // A card with no height - the rail hidden - measures nothing.
        r.hidden = at(L, 0, four);
        // The rail widened: the roomy card, measured, three in the same column.
        r.roomy = at(L, 58, four);
    """))

    assert r["compact"] == [
        _cards(PLAN_CARDS, ["BLR", "YVR", "DEL", "JFK"], head="BLR"),
        _cards(PLAN_CARDS, ["BLR", "YVR", "DEL"], head="BLR"),
        _cards(REPORT_CARDS, ["LHR", "SIN", "NRT", "SYD"], head="SYD"),
    ]
    assert r["twoByTwo"] == [4, 4]
    assert r["redrawn"] == r["compact"][0]
    assert r["hidden"] == r["compact"][0]
    assert r["roomy"] == _cards(PLAN_CARDS, ["BLR", "YVR", "DEL"], head="BLR")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_hand_built_rail_is_drawn_as_far_as_it_can_be(client):
    page = _page(client)
    junk = {"type": "rail", "side": "plan", "title": None, "cards": [
        {"id": "a", "start": "2026-10-20T00:00:00Z", "end": "2026-10-21T00:00:00Z",
         "out": "<b>CX1</b>", "in": "<i>CX2</i>", "port": "<u>P</u>"},
        {"out": "no start"}, {"start": "not a time"}, {"start": "2026-10-20T00:00:00Z", "end": "not a time"},
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-19T00:00:00Z"}, None, "card", 5,
        {"start": "2026-10-16T00:00:00Z", "end": None, "port": "Open"},
        {"start": "2026-10-17T00:00:00Z", "end": "2026-10-17T06:00:00Z"},
    ]}
    r = _node(_rails_script(page, f"""
        // Unsynced: drawn for the time it's drawn at - the clock, 12:07:30Z
        // on 18 October - and no focus group started for it.
        const junk = {json.dumps(junk)};
        const box = renderSpec(junk, 'X', 'x');
        r.junk = rail(box);
        r.html = [box, ...box.querySelectorAll('*')].map(n => n._html).join('');
        r.groups = [focusGroups.size, timers.length];
        // The same cards looking back: the latest over by then at the foot,
        // held full; a side that's neither, ahead.
        r.report = rail(renderSpec({{...junk, side: 'report'}}, 'X', 'x')).cards;
        r.sideways = rail(renderSpec({{...junk, side: 'sideways'}}, 'X', 'x')).cards;
        r.none = rail(renderSpec({{type: 'rail', side: 'report', title: 'Report', cards: []}}, 'X', 'x'));
        r.bare = rail(renderSpec({{type: 'rail'}}, 'X', 'x'));
        // A frame's side that isn't a rail is none; a frame's timeline that
        // isn't one is drawn as renderSpec draws it, the rails beside it.
        r.sides = renderSpec({{type: 'rails', left: {{type: 'timeline'}}, timeline: {json.dumps(TIMELINE)}, right: 'x'}}, 'X', 'x')
          .children.map(c => c.className);
        const odd = renderSpec({{type: 'rails', left: {{type: 'rail', cards: []}}, timeline: null}}, 'X', 'x');
        r.odd = [odd.children.map(c => c.className), rail(odd.children[0]).rows];
        r.days = [fmtCardDay(Date.parse('2026-12-31T22:00:00Z')), fmtCardDay(Date.parse('2027-01-01T03:00:00Z'))];
    """))

    # Only the cards it can place, in time order, the head its top - the
    # rest left out: no start, a time that isn't one, an end before its
    # start, not a card. A missing flight a dash, a missing port nothing;
    # every text as text.
    assert r["junk"]["cards"] == [">— Open — 16 Oct", "—  — 17 Oct", "<b>CX1</b> <u>P</u> <i>CX2</i> 20 Oct"]
    assert r["junk"]["title"] == " "
    assert r["junk"]["rows"] == ["rail-head", "rail-cards"]
    assert r["html"] == ""
    assert r["groups"] == [0, 0]
    assert r["report"] == ["— Open — 16 Oct", "—  — 17 Oct", "><b>CX1</b> <u>P</u> <i>CX2</i> 20 Oct"]
    assert r["sideways"] == r["junk"]["cards"]
    # No cards: said, under its heading.
    assert (r["none"]["title"], r["none"]["none"], r["none"]["cards"]) == ("Report", "None", [])
    assert (r["bare"]["none"], r["bare"]["cards"]) == ("None", [])
    assert r["sides"] == ["spec-timeline"]
    assert r["odd"] == [["spec-rail", "spec-msg"], ["rail-head", "rail-cards"]]
    # A card's day, the one it left, in UTC.
    assert r["days"] == ["31 Dec", "01 Jan"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_rails_off_the_page_are_let_go_of_with_their_timeline(client):
    page = _page(client)
    r = _node(_rails_script(page, f"""
        const box = renderSpec({json.dumps(RAILS)}, 'Roster', 'roster');
        box.querySelectorAll('.rail-cards').forEach(l => {{ l.isConnected = true; }});
        box.querySelector('.tl-scroll').isConnected = true;
        r.before = [timelines.size, focusGroups.get('frms').members.size];
        // The tile drawn afresh: the old face off the page.
        box.querySelectorAll('.rail-cards').forEach(l => {{ l.isConnected = false; }});
        box.querySelector('.tl-scroll').isConnected = false;
        releaseTimelines();
        r.after = [timelines.size, focusGroups.get('frms').members.size, observers.map(o => o.disconnected)];
        // A rail's own observer, finding its list gone, lets go of it too.
        const again = renderSpec({json.dumps(PLAN)}, 'Roster', 'roster');
        r.joined = [timelines.size, focusGroups.get('frms').members.size];
        observers[observers.length - 1].callback([]);
        r.gone = [timelines.size, focusGroups.get('frms').members.size, observers[observers.length - 1].disconnected];
    """))

    assert r["before"] == [3, 3]
    assert r["after"] == [0, 0, [1, 1, 1]]
    assert r["joined"] == [1, 1]
    assert r["gone"] == [0, 0, 1]


def _css_rules(page):
    """The page's CSS as far as the rails' tests need it: (base, blocks) -
    the rules outside any at-rule, as text, and each single-condition
    @container (max-width|max-height:Npx) block as (feature, px, its
    rules), in the page's order. Comments go first (they hold braces)."""
    css = re.sub(r"/\*.*?\*/", "", page[page.index("<style>") + len("<style>") : page.index("</style>")], flags=re.S)
    base, blocks, at = [], [], 0
    for m in re.finditer(r"@[\w-]+[^{;]*\{", css):
        if m.start() < at:
            continue
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        base.append(css[at : m.start()])
        cond = re.fullmatch(r"@container \(max-(width|height):(\d+)px\)\s*\{", m.group(0))
        if cond:
            blocks.append((cond.group(1), int(cond.group(2)), css[m.end() : i - 1]))
        at = i
    base.append(css[at:])
    return "".join(base), blocks


def _decls(text, selector):
    """What every rule in text naming selector declares, later ones winning."""
    out = {}
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        if selector in [x.strip() for x in sels.split(",")]:
            out.update((k.strip(), v.strip()) for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d))
    return out


def _styled(css, selector, **size):
    """What selector gets in a container of size (width=, height=, px): its
    rules outside any at-rule, then each @container block the size is
    within - a feature not given (an inline-size container's height)
    never matching."""
    base, blocks = css
    out = _decls(base, selector)
    for feature, px, body in blocks:
        if feature in size and size[feature] <= px:
            out.update(_decls(body, selector))
    return out


def _px(v):
    return float(re.fullmatch(r"(-?[\d.]+)px", v).group(1))


def _tile_content(css, w, h, cell):
    """A w x h tile's content box at a cell size: its span of cells and the
    gaps between, less its padding and border (box-sizing border-box)."""
    root = _decls(css[0], ":root")
    gap = _px(root["--tile-gap"])
    tile = _decls(css[0], ".tile")
    pad_y, pad_x = (_px(v) for v in tile["padding"].split())
    border = _px(tile["border"].split()[0])
    return (w * cell + (w - 1) * gap - 2 * (pad_x + border), h * cell + (h - 1) * gap - 2 * (pad_y + border))


def _rail_width(css, tile_w):
    """A rail's width in a tile tile_w wide: its clamp(lo, N cqi, hi), cqi the tile's."""
    lo, cqi, hi = re.fullmatch(r"clamp\(([\d.]+)px, ([\d.]+)cqi, ([\d.]+)px\)",
                               _decls(css[0], ".spec-rails > .spec-rail")["width"]).groups()
    return min(max(float(lo), float(cqi) * tile_w / 100), float(hi))


def test_the_rails_are_left_out_of_a_tile_too_small_to_hold_them(client):
    page = _page(client)
    css = page[: page.index("</style>")]
    # Under 310px wide or 240px tall inside, the rails go and the timeline
    # has the tile, as it's drawn without them; otherwise it takes what
    # the rails leave.
    assert "@container (max-width:309px){ .spec-rails > .spec-rail{display:none;} }" in css
    assert "@container (max-height:239px){ .spec-rails > .spec-rail{display:none;} }" in css
    assert ".spec-panel > .spec-rails{flex:1 1 0;}" in css
    assert ".spec-rails > .spec-timeline{flex:1 1 0;}" in css
    assert ".spec-rails > .spec-rail{flex:none;width:clamp(84px, 22cqi, 156px);}" in css
    # Every colour a token: a card's edge the bands' amber, its surface
    # the frame's, the head outlined in the accent.
    rails = css[css.index("/* rails: a timeline flanked") : css.index("/* panel: its children, stacked */")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rails)
    assert "border-left:3px solid var(--layover);" in rails
    assert "background:var(--inset);" in rails
    assert ".rail-card.head{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);}" in rails
    # Four corners: a two-by-two grid, the port in the display face.
    assert "display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-rows:1fr 1fr;" in rails
    assert "var(--font-display)" in rails[rails.index(".rc-port{") :].split("}")[0]
    # No count, no tint by the cursor, no range or tag.
    for gone in ("rail-count", ".at", "rc-label", "rc-tag", "rc-when", "rc-detail"):
        assert gone not in rails, gone
    assert "rail-count" not in page and "fmtCardDays" not in page
    # A card's size and the gap between two are the CSS's; the script's
    # constants - what fit counts with before a card's measured - agree.
    rules = _css_rules(page)
    assert _decls(rules[0], ".rail-card")["height"] == f"{_js(page, 'RAIL_CARD_PX').split('= ')[1].split(';')[0]}px"
    assert _decls(rules[0], ".rail-cards")["gap"] == f"{_js(page, 'RAIL_GAP_PX').split('= ')[1].split(';')[0]}px"


def test_the_rails_show_down_to_a_two_wide_tile_with_a_compact_card(client):
    page = _page(client)
    css = _css_rules(page)
    root = _decls(css[0], ":root")
    big, small = _px(root["--tile-cell-max"]), _px(root["--tile-cell-min"])
    # The hide rules key off the tile - the rail's nearest container
    # but itself - and the card's off the rail, a container of its own.
    assert _decls(css[0], ".tile")["container-type"] == "size"
    assert _decls(css[0], ".spec-rail")["container-type"] == "inline-size"
    shown = lambda w, h: _styled(css, ".spec-rails > .spec-rail", width=w, height=h).get("display") != "none"
    card = lambda rail_w: _styled(css, ".rail-card", width=rail_w)
    corner = lambda cls, rail_w: _styled(css, cls, width=rail_w)

    # A 2x2 at a large viewport (cells at their max), as Chromium lays it
    # out - 334 x 338 inside: the rails show, each at its 84px min, with
    # the compact card.
    two = _tile_content(css, 2, 2, big)
    assert two == (334, 338)
    assert shown(*two)
    assert _rail_width(css, two[0]) == 84
    compact = card(84)
    assert (compact["height"], compact["padding"], compact["border-left-width"], compact["border-radius"],
            compact["column-gap"]) == ("46px", "3px 6px", "2px", "7px", "4px")
    assert [(corner(c, 84)["font-size"], corner(c, 84)["line-height"]) for c in (".rc-out", ".rc-in", ".rc-port", ".rc-day")] \
        == [("10px", "13px"), ("10px", "13px"), ("13px", "15px"), ("9px", "13px")]
    # Compact to a rail 119px wide; roomy past it.
    assert card(119)["height"] == "46px" and card(120)["height"] == "58px"

    # A 4-wide tile at a large viewport: the rail at its 156px max, the
    # roomy card exactly as it was - nothing compact reaches it.
    four = _tile_content(css, 4, 3, big)
    assert shown(*four)
    assert _rail_width(css, four[0]) == 156
    roomy = card(156)
    assert (roomy["height"], roomy["padding"], roomy["border-left"], roomy["border-radius"], roomy["column-gap"]) \
        == ("58px", "4px 8px 4px 7px", "3px solid var(--layover)", "var(--radius-sm)", "6px")
    assert "border-left-width" not in roomy
    assert [corner(c, 156)["font"] for c in (".rc-out", ".rc-in", ".rc-port", ".rc-day")] == [
        "600 11.5px/16px var(--font-mono)", "500 11.5px/16px var(--font-mono)",
        "800 17px/20px var(--font-display)", "500 10.5px/16px var(--font-mono)"]
    assert all("font-size" not in corner(c, 156) for c in (".rc-out", ".rc-in", ".rc-port", ".rc-day"))
    # A 4-wide tile on a small screen (cells at their min): its rail at the
    # min too, so the compact card.
    assert _rail_width(css, _tile_content(css, 4, 3, small)[0]) == 84

    # Left out under 310px wide - a 2x2 with the cells at their min among
    # them - or 240px tall - a 2x1.
    assert shown(310, two[1]) and not shown(309, two[1])
    assert shown(two[0], 240) and not shown(two[0], 239)
    assert not shown(*_tile_content(css, 2, 2, small))
    assert not shown(*_tile_content(css, 2, 1, big))
    # And four columns fit in the narrowest tile they show in: two rails at
    # their min and the gaps leave the timeline's two lanes room (Chromium:
    # 57px each at 310px).
    narrowest = next(w for w in range(200, 800) if shown(w, two[1]))
    gap = _px(_decls(css[0], ".spec-rails")["gap"])
    assert narrowest - 2 * _rail_width(css, narrowest) - 2 * gap >= 120


# ---------- the timeline's tweaks: Now in the frame, day lines, 6h steps ----------
#
# In a rails frame the timeline's Now button is the frame's, at its
# top-right, and the readout has the focus row, centred over the lanes;
# alone, a timeline keeps Now in its row. Every timeline draws a faint
# line at each midnight (UTC) under everything else, and scrolls six
# hours at a time: a wheel's notch or an arrow key moves the time at the
# viewport's middle - the cursor's, synced - to the next 6h mark, and a
# scroll by hand settles on the nearest once it ends. Under node, over
# the stand-in DOM, with the clock held still as for the focus clock;
# the browser's part - the scroll event a scroll fires, its scrollend -
# called as a browser would.

SIX_HOURS_PX_480 = 6 * 3600000 * 480 / (7 * 86400000)   # a 6h mark's spacing in a 480px viewport: a week to its height


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_in_a_rails_frame_the_now_button_is_the_frames_at_its_top_right(client):
    page = _page(client)
    untitled = {**RAILS, "timeline": {**SYNCED, "title": None}}
    r = _node(_rails_script(page, f"""
        const box = renderSpec({json.dumps(RAILS)}, 'Roster', 'roster');
        const [L, tl, R, slot] = box.children;
        r.kids = box.children.map(c => c.className);
        // The slot: a spacer for the title's line, then the button - so
        // it's level with the focus row, whose readout is all it holds.
        r.slot = slot.children.map(c => [c.className, c.textContent]);
        r.focus = tl.querySelector('.tl-focus').children.map(c => c.className);
        r.inTimeline = tl.querySelectorAll('.tl-nowbtn').length;
        const btn = slot.children[1];
        r.btn = [btn.tag, btn.type, btn.textContent, btn.title, btn.hidden];
        // The same button: hidden while the group follows now, shown once
        // the timeline's scrolled by hand, and its click brings the group back.
        show(tl, 480);
        scrollTo(tl, (Date.parse('2026-10-14T03:00:00Z') - {TIMELINE_T0}) * 480 / WEEK);
        r.scrolled = [btn.hidden, focusGroups.get('frms').following, tl.querySelector('.tl-readout').textContent];
        btn.onclick();
        r.back = [btn.hidden, focusGroups.get('frms').following, tl.querySelector('.tl-readout').textContent];
        // Untitled, no spacer; unsynced, no Now to move.
        r.untitled = renderSpec({json.dumps(untitled)}, 'Roster', 'roster').children[3].children.map(c => c.className);
        r.unsynced = renderSpec({{...{json.dumps(RAILS)}, timeline: {json.dumps(TIMELINE)}}}, 'Roster', 'roster')
          .children.map(c => c.className);
        // Alone, a synced timeline keeps Now at its focus row's end.
        r.alone = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster').querySelector('.tl-focus').children.map(c => c.className);
    """))

    assert r["kids"] == ["spec-rail", "spec-timeline", "spec-rail", "rails-now"]
    assert r["slot"] == [["rail-sp t", " "], ["btn sm tl-nowbtn", "Now"]]
    assert r["focus"] == ["tl-readout"]
    assert r["inTimeline"] == 0
    assert r["btn"] == ["button", "button", "Now", "Back to now, and follow it", True]
    assert r["scrolled"] == [False, False, "Wed 14 Oct 03:00Z"]
    assert r["back"] == [True, True, "Sun 18 Oct 12:00Z"]
    assert r["untitled"] == ["btn sm tl-nowbtn"]
    assert r["unsynced"] == ["spec-rail", "spec-timeline", "spec-rail"]
    assert r["alone"] == ["tl-readout", "btn sm tl-nowbtn"]

    # The frame places it: at its top-right, over the Report rail - the
    # frame its containing block - the button alone taking a click.
    css = _css_rules(page)
    root = _decls(css[0], ":root")
    big, small = _px(root["--tile-cell-max"]), _px(root["--tile-cell-min"])
    assert _decls(css[0], ".spec-rails")["position"] == "relative"
    slot = _decls(css[0], ".rails-now")
    assert (slot["position"], slot["top"], slot["right"], slot["pointer-events"]) == ("absolute", "0", "0", "none")
    assert (slot["flex-direction"], slot["align-items"], slot["gap"]) == ("column", "flex-end", "4px")
    assert _decls(css[0], ".rails-now > .tl-nowbtn")["pointer-events"] == "auto"
    # As small as it was in the row, wherever it is - over .btn.sm's size.
    btn = _decls(css[0], ".btn.tl-nowbtn")
    assert (btn["padding"], btn["font-size"], btn["line-height"]) == ("1px 8px", "11px", "16px")
    assert _decls(css[0], ".btn.tl-nowbtn[hidden]")["display"] == "none"
    # The readout centred over the lanes - the row keeping their
    # scrollbar gutter - with the rails shown: a 2x2, a 4x3.
    focus = lambda w, h: _styled(css, ".spec-rails .tl-focus", width=w, height=h)
    for size in (_tile_content(css, 2, 2, big), _tile_content(css, 4, 3, big)):
        f = focus(*size)
        assert (f["justify-content"], f["scrollbar-gutter"], f["overflow"]) == ("center", "stable", "hidden"), size
        assert "padding-right" not in f
    # With them left out, the row as it is alone: the readout at its
    # start, room for Now at its end.
    for size in ((309, 338), (334, 239), _tile_content(css, 2, 1, big), _tile_content(css, 2, 2, small)):
        f = focus(*size)
        assert (f["justify-content"], f["scrollbar-gutter"], f["padding-right"]) == ("flex-start", "auto", "48px"), size
    assert _decls(css[0], ".tl-focus .tl-nowbtn")["margin-left"] == "auto"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_timeline_draws_a_line_at_each_midnight_under_everything(client):
    page = _page(client)
    lane = lambda a, b: {"label": None, "intervals": [{"start": a, "end": b, "label": "X"}]}
    within = {"type": "timeline", "lanes": [lane("2026-10-10T01:00:00Z", "2026-10-10T14:00:00Z")]}
    across = {"type": "timeline", "lanes": [lane("2026-10-10T20:00:00Z", "2026-10-11T04:00:00Z")]}
    on_midnight = {"type": "timeline", "lanes": [lane("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z")]}
    decade = {"type": "timeline", "lanes": [lane("2020-01-01T00:00:00Z", "2030-01-07T00:00:00Z")]}
    ages = {"type": "timeline", "lanes": [lane("0001-01-01T00:00:00Z", "9999-01-01T00:00:00Z")]}
    r = _node(_rails_script(page, f"""
        const lines = box => {{
          const track = box.querySelector('.tl-track');
          const grid = kids(track, 'tl-grid');
          return {{order: track.children.map(c => c.className), layers: grid.length,
                   tops: grid.flatMap(g => kids(g, 'tl-gridline')).map(g => parseFloat(g.style.top))}};
        }};
        const plain = renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster');
        show(plain, 480);
        r.plain = lines(plain);
        // Resized: laid out again with the blocks, on the new scale.
        show(plain, 600);
        r.resized = lines(plain);
        // Synced: padded half a frame, as the blocks are.
        const inGroup = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        show(inGroup, 480);
        r.synced = lines(inGroup);
        r.firstBlock = parseFloat(inGroup.querySelector('.tl-block').style.top);
        // Built, not shown: at the fallback height, a week to 240px.
        r.within = lines(renderSpec({json.dumps(within)}, 'X', 'x'));
        r.across = lines(renderSpec({json.dumps(across)}, 'X', 'x'));
        r.onMidnight = lines(renderSpec({json.dumps(on_midnight)}, 'X', 'x'));
        // Ten years (3660 midnights, both ends among them): a line each; a
        // hand-built spec spanning ages: none, rather than a node a day.
        r.decade = lines(renderSpec({json.dumps(decade)}, 'X', 'x')).tops.length;
        r.ages = lines(renderSpec({json.dumps(ages)}, 'X', 'x')).layers;
        r.html = [plain, ...plain.querySelectorAll('*')].map(n => n._html).join('');
    """))

    t0 = _ms("2026-10-10T01:00:00Z")
    midnights = [_ms(f"2026-10-{d:02d}T00:00:00Z") for d in range(11, 23)]   # 11 to 22 October: the axis runs to 10:00Z on the 22nd
    at = lambda h, pad=0: [pytest.approx(pad + (m - t0) * h / (7 * 86400000)) for m in midnights]
    # One layer of lines, first in the track - under the lanes, so under
    # their bands, the grey and the blocks, and the now line over it -
    # a line at each midnight from the axis' start to its end.
    assert r["plain"] == {"order": ["tl-grid", "tl-lane l0", "tl-lane l1", "tl-now"], "layers": 1, "tops": at(480)}
    assert r["resized"]["tops"] == at(600)
    assert r["synced"]["tops"] == at(480, pad=240)
    assert r["firstBlock"] == 240
    # An axis within a day draws none - not even the layer; across one
    # midnight, one; from a midnight, one at its top and one a day on.
    assert r["within"] == {"order": ["tl-lane l0"], "layers": 0, "tops": []}
    assert r["across"]["tops"] == [pytest.approx(4 * 3600000 * 240 / (7 * 86400000))]
    assert r["onMidnight"]["tops"] == [0, pytest.approx(240 / 7)]
    assert r["decade"] == 3660 and r["ages"] == 0
    assert r["html"] == ""

    # Faint, in its own token in every theme; under everything, and never
    # in the way of a tap.
    css = page[: page.index("</style>")]
    light = css[css.index(":root{") : css.index("@media (prefers-color-scheme:dark)")]
    os_dark = css[css.index("@media (prefers-color-scheme:dark)") : css.index(':root[data-theme="dark"]{')]
    toggle_dark = css[css.index(':root[data-theme="dark"]{') :]
    toggle_dark = toggle_dark[: toggle_dark.index("}")]
    assert "--grid:color-mix(in srgb, var(--muted) 22%, transparent);" in light
    for block in (os_dark, toggle_dark):
        assert "--grid:color-mix(in srgb, var(--muted) 26%, transparent);" in block
    rules = _css_rules(page)[0]
    layer, line = _decls(rules, ".tl-grid"), _decls(rules, ".tl-gridline")
    assert (layer["position"], layer["inset"], layer["pointer-events"]) == ("absolute", "0", "none")
    assert (line["position"], line["left"], line["right"], line["border-top"]) == ("absolute", "0", "0", "1px solid var(--grid)")


STEP_HARNESS = r"""
    // A wheel event as a browser sends it to the frame's listeners - then,
    // if it scrolled, the scroll event that fires - and where it left the
    // group: how far it moved, in 6h marks, and the focus.
    const MARK = 6 * 3600000;
    let prevented = 0;
    const where = (g, sc, before, px) => ({moved: (sc.scrollTop - before) / (MARK * px),
      focus: new Date(g.focusMs).toISOString().slice(5, 16), following: g.following});
    const wheel = (box, px, deltaY, timeStamp, more) => {
      const sc = box.querySelector('.tl-scroll'), before = sc.scrollTop, g = focusGroups.get('frms');
      const e = {deltaY, deltaMode: 0, timeStamp, ctrlKey: false, preventDefault(){ prevented++; }, ...(more || {})};
      sc.listeners.wheel.forEach(l => l.f(e));
      if(sc.scrollTop!==before) sc.onscroll();
      return where(g, sc, before, px);
    };
    const key = (box, px, k, more) => {
      const sc = box.querySelector('.tl-scroll'), before = sc.scrollTop, g = focusGroups.get('frms');
      sc.onkeydown({key: k, preventDefault(){ prevented++; }, ...(more || {})});
      if(sc.scrollTop!==before) sc.onscroll();
      return where(g, sc, before, px);
    };
"""


def _step_script(page, scenario):
    return (
        MINI_DOM + "\n".join(_js(page, n) for n in RAILS_PARTS) + TIMELINE_HARNESS + SYNC_HARNESS + RAILS_HARNESS
        + STEP_HARNESS + scenario + "\nconsole.log(JSON.stringify(r));"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_wheel_notch_or_an_arrow_key_steps_the_timeline_six_hours(client):
    page = _page(client)
    r = _node(_step_script(page, f"""
        const box = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        const sc = show(box, 480);
        const scale = 480 / WEEK;
        r.listeners = sc.listeners.wheel.map(l => l.options);
        // On now, a mark (12:00Z): a notch down, then another after a pause.
        r.notch = wheel(box, scale, 100, 1000);
        r.again = wheel(box, scale, 100, 2000);
        // Two more, quick: a step each.
        r.quick = [wheel(box, scale, 100, 2030), wheel(box, scale, 100, 2060)];
        // A trackpad's short swipe - eight small events - is one step;
        // running on, a step for each notch's travel after its first.
        r.swipe = [0, 1, 2, 3, 4, 5, 6, 7].map(i => wheel(box, scale, 10, 5000 + 16 * i).moved);
        r.runOn = [8, 9, 10].map(i => wheel(box, scale, 10, 5000 + 16 * i).moved);
        r.afterSwipe = focusGroups.get('frms').focusMs;
        // Turned back: a new gesture, a step back at once.
        r.back = wheel(box, scale, -10, 5176);
        // A notch counted in lines (Firefox's three), and one on the mark
        // the browser rounds a scroll to: the pixel - still on it.
        r.lines = [wheel(box, scale, 3, 9000, {{deltaMode: 1}}), wheel(box, scale, 3, 9050, {{deltaMode: 1}})];
        sc.listeners.wheel.forEach(l => l.f({{deltaY: 100, deltaMode: 0, timeStamp: 12000, preventDefault(){{ prevented++; }}}}));
        const exact = sc.scrollTop;
        sc.scrollTop = Math.round(sc.scrollTop);
        sc.onscroll();
        r.rounded = [exact, sc.scrollTop, new Date(tlSnap({TIMELINE_T0} + sc.scrollTop / scale)).toISOString().slice(5, 16),
                     new Date(focusGroups.get('frms').focusMs).toISOString().slice(5, 16)];
        r.prevented = prevented;
        // A pinch, or a sideways turn: the browser's - not stepped, not prevented.
        r.pinch = wheel(box, scale, 50, 14000, {{ctrlKey: true}});
        r.sideways = wheel(box, scale, 0, 15000, {{deltaX: 40}});
        r.preventedAfter = prevented;
        // Off a mark - Now on 12:15Z - a notch down to the next mark, 18:00Z;
        // up, to the last, 12:00Z.
        clock = Date.parse('2026-10-18T12:20:00Z');
        box.querySelector('.tl-nowbtn').onclick(); sc.onscroll();
        r.offDown = wheel(box, scale, 100, 20000).focus;
        box.querySelector('.tl-nowbtn').onclick(); sc.onscroll();
        r.offUp = wheel(box, scale, -100, 21000).focus;
        // The arrow keys step it too; the others, and a modified arrow, aren't its.
        r.keys = [key(box, scale, 'ArrowDown'), key(box, scale, 'ArrowDown'), key(box, scale, 'ArrowUp'),
                  key(box, scale, 'ArrowDown', {{shiftKey: true}}).moved, key(box, scale, 'PageDown').moved];
        // Unsynced: the time at the viewport's middle steps, mark to mark.
        const plain = renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster');
        const ps = show(plain, 480);
        ps.onscroll();   // its own scroll, to open on now
        const middle = () => new Date({TIMELINE_T0} + (ps.scrollTop + 240) / scale).toISOString().slice(5, 16);
        r.plain = [middle()];
        ps.listeners.wheel.forEach(l => l.f({{deltaY: 100, deltaMode: 0, timeStamp: 30000, preventDefault(){{}}}}));
        ps.onscroll();
        r.plain.push(middle());
        ps.onkeydown({{key: 'ArrowUp', preventDefault(){{}}}});
        r.plain.push(middle());
    """))

    step = lambda n, focus, following=False: {"moved": pytest.approx(n), "focus": focus, "following": following}
    # A listener that can stop the browser's own scroll.
    assert r["listeners"] == [{"passive": False}]
    # A notch: one 6h mark, the group's focus on it, no longer following.
    assert r["notch"] == step(1, "10-18T18:00")
    assert r["again"] == step(1, "10-19T00:00")
    assert r["quick"] == [step(1, "10-19T06:00"), step(1, "10-19T12:00")]
    # Eight small events, one step; three more - a notch's travel on - one more.
    assert r["swipe"] == [pytest.approx(1)] + [0] * 7
    assert r["runOn"] == [0, 0, pytest.approx(1)]
    assert r["back"] == step(-1, "10-19T18:00")
    assert r["lines"] == [step(1, "10-20T00:00"), step(1, "10-20T06:00")]
    # A step, its scroll rounded to the pixel - a few minutes short of the
    # mark, so the quarter hour under it the one before: the mark.
    exact, rounded, floored, focus = r["rounded"]
    assert exact == pytest.approx(717 + 1 / 7) and rounded == 717
    assert (floored, focus) == ("10-20T11:45", "10-20T12:00")
    assert r["prevented"] == 1 + 1 + 2 + 8 + 3 + 1 + 2 + 1
    assert r["pinch"] == step(0, "10-20T12:00") and r["sideways"] == step(0, "10-20T12:00")
    assert r["preventedAfter"] == r["prevented"]
    assert (r["offDown"], r["offUp"]) == ("10-18T18:00", "10-18T12:00")
    assert r["keys"] == [step(1, "10-18T18:00"), step(1, "10-19T00:00"), step(-1, "10-18T18:00"), 0, 0]
    # A step is a mark's spacing at the frame's scale: 6h of a week to 480px.
    assert SIX_HOURS_PX_480 == pytest.approx(480 / 28)
    assert r["plain"] == ["10-18T12:00", "10-18T18:00", "10-18T12:00"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_scroll_by_hand_settles_on_the_nearest_six_hour_mark(client):
    page = _page(client)
    r = _node(_step_script(page, f"""
        const a = renderSpec({json.dumps(SYNCED)}, 'Roster', 'roster');
        const b = renderSpec({json.dumps(OTHER)}, 'Sleep', 'sleep');
        const sa = show(a, 480), sb = show(b, 300);
        const g = focusGroups.get('frms');
        const focus = () => [new Date(g.focusMs).toISOString().slice(5, 16), g.following];
        // The browser's end of a scroll - and the scroll event of any
        // scroll it then makes.
        const end = sc => {{ const before = sc.scrollTop; sc.onscrollend(); if(sc.scrollTop!==before) sc.onscroll(); return sc.scrollTop!==before; }};
        // Dragged by hand to 14:40Z on Monday 12 October: the quarter
        // hour under it, until the drag ends - then the nearest mark,
        // 12:00Z, for the whole group.
        scrollTo(a, (Date.parse('2026-10-12T14:40:00Z') - {TIMELINE_T0}) * 480 / WEEK);
        r.dragged = focus();
        r.settled = [end(sa), focus(), synced(a, {TIMELINE_T0}, 480).atCursor, synced(b, {OTHER_T0}, 300).atCursor];
        // Its settle's own end, and b's - scrolled to the time, not by
        // hand: nothing moves.
        r.still = [end(sa), (sb.onscroll(), end(sb)), focus()];
        // Now: on now, a quarter hour off the marks, following - and the
        // end of its scroll leaves it there.
        clock = Date.parse('2026-10-18T12:20:00Z');
        a.querySelector('.tl-nowbtn').onclick(); sa.onscroll(); sb.onscroll();
        r.now = [end(sa), end(sb), focus()];
        // A hand's scroll, then Now before the scroll's end: Now outdoes it.
        scrollTo(a, (Date.parse('2026-10-13T08:00:00Z') - {TIMELINE_T0}) * 480 / WEEK);
        a.querySelector('.tl-nowbtn').onclick(); sa.onscroll();
        r.outdone = [end(sa), focus()];
        // The follow tick's scroll, to 12:30Z: its own, never settled.
        clock = Date.parse('2026-10-18T12:30:00Z');
        timers[timers.length - 1].f(); sa.onscroll();
        r.ticked = [end(sa), focus()];
        // Unsynced: its own scroll (opening on now) isn't settled; a hand's
        // settles the time at its middle on the nearest mark.
        const plain = renderSpec({json.dumps(TIMELINE)}, 'Roster', 'roster');
        const ps = show(plain, 480);
        ps.onscroll();
        r.plainOwn = end(ps);
        ps.scrollTop += 37; ps.onscroll();
        const middle = () => {TIMELINE_T0} + (ps.scrollTop + 240) / (480 / WEEK);
        r.plainHand = [end(ps), new Date(middle()).toISOString().slice(5, 16)];
    """))

    assert r["dragged"] == ["10-12T14:30", False]
    assert r["settled"] == [True, ["10-12T12:00", False], "2026-10-12T12:00:00.000Z", "2026-10-12T12:00:00.000Z"]
    assert r["still"] == [False, False, ["10-12T12:00", False]]
    assert r["now"] == [False, False, ["10-18T12:15", True]]
    assert r["outdone"] == [False, ["10-18T12:15", True]]
    assert r["ticked"] == [False, ["10-18T12:30", True]]
    assert r["plainOwn"] is False
    # 37px on from now (12:00Z), at a week to 480px, is 00:57Z the next
    # day: 00:00Z is nearest.
    assert r["plainHand"] == [True, "10-19T00:00"]


def test_lane_headings_fit_complete_in_a_two_by_two(client):
    page = _page(client)
    css = _css_rules(page)
    root = _decls(css[0], ":root")
    big, small = _px(root["--tile-cell-max"]), _px(root["--tile-cell-min"])
    heading = lambda w, h: _styled(css, ".tl-lh", width=w, height=h)
    two, four = _tile_content(css, 2, 2, big), _tile_content(css, 4, 3, big)

    # A 4-wide tile's headings exactly as they were.
    assert (heading(*four)["font-size"], heading(*four)["letter-spacing"]) == ("10.5px", ".8px")
    assert "line-height" not in heading(*four)
    # A 2-wide tile's - and a 4-wide one a small screen shrinks - set tighter.
    for size in (two, _tile_content(css, 4, 3, small)):
        assert (heading(*size)["font-size"], heading(*size)["letter-spacing"]) == ("10px", ".3px"), size

    # COMPLETE in a 2x2's lane: the timeline what its rails and the gaps
    # leave, less the heading row's padding and a thin scrollbar's gutter
    # (Chromium's, 10px), halved, less a heading's padding. Its width is
    # 5.51em of the face (Inter at 600 - system-ui where IBM Plex Sans
    # isn't installed - measured in Chromium) and a letter-space a letter.
    def room(width):
        frame = width - 2 * _rail_width(css, width) - 2 * _px(_decls(css[0], ".spec-rails")["gap"])
        head = sum(_px(p) for p in _decls(css[0], ".tl-head")["padding"].split()[1:2]) * 2
        pad = _px(_decls(css[0], ".tl-lh")["padding"].split()[1]) * 2
        return (frame - head - 10) / 2 - pad

    complete = lambda h: 5.51 * _px(h["font-size"]) + 8 * _px(h["letter-spacing"])
    assert room(two[0]) == pytest.approx(61)
    assert complete(heading(*two)) <= room(two[0]) - 3        # fits, with room to spare
    assert complete(_decls(css[0], ".tl-lh")) > room(two[0])  # as it was, it didn't
    # On the 10.5px heading's line: the row - and the frame under it -
    # stays level with the rails' headings.
    assert heading(*two)["line-height"] == "calc(10.5px * 1.45)"
    assert _decls(css[0], "body")["line-height"] == "1.45"
    assert _decls(css[0], ".rail-head")["font-size"] == "10.5px"


# ---------- an uninstalled app's data: Remove data ----------
#
# Uninstall keeps what an app wrote; GET /api/app-sources lists every
# app source, orphaned or not, DELETE /api/app-sources/<id> removes an
# orphan's data and DELETE /api/app-sources/<id>/<metric> one metric of
# it. The page shows an orphan in Connected sources - a red Remove all
# data, and, opened, a Remove data per metric it left behind - and under
# a Your data row's source list, a Remove data for that row's metric;
# each behind one confirm. A live app's source gets no control. The
# page's own functions run under node over the stand-in DOM, against the
# listings and the reply the server gives.

ORPHAN_PARTS = [
    "$", "el", "num", "orphans", "loadAppSources", "orphanRow", "orphanMetrics", "countOf",
    "appDataSummary", "openRemoveAppData", "openModal", "paintSources", "orphanLines", "fillSources",
    "sourceList", "expandable",
]

ORPHAN_HARNESS = r"""
    const copy = x => JSON.parse(JSON.stringify(x));
    // The server, as the test left it: the listing before and after the
    // removal, and the removal's reply, as send() hands it on.
    const BEFORE_ = BEFORE, AFTER_ = AFTER, REPLY_ = REPLY;
    let listing = BEFORE_;
    const calls = [], loads = [];
    const api = {
      appSources: async () => copy(listing),
      removeAppSource: async id => { calls.push(id); listing = AFTER_; return copy(REPLY_); },
      removeAppSourceMetric: async (id, metric) => {
        calls.push([id, metric]); listing = AFTER_; return copy(REPLY_);
      },
    };
    const loadData = () => loads.push('data'), loadHome = () => loads.push('home');
    // The rest of the card, which these tests don't look at.
    const cardTitle = t => el('h3', null, esc(t));
    const sourceRow = c => { const row = el('div', 'src'); row.dataset.id = c.id; return row; };
    const addSourceRow = () => el('div', 'src-add');
    const repaintPanel = () => {};
    const metricName = k => ({weight_kg:'Weight', steps:'Steps'})[k] || k;
    const srcName = id => id, makeSortable = () => {};
    const state = {connectors:[{id:'garmin', name:'Garmin', connected:true}], appSources:null,
                   data:null, roles:{}, note:{}, open:new Set(), orphanOpen:new Set()};
    const card = document.body.appendChild(mk('section'));
    card.id = 'sourcesCard';
    const r = {};
    const settle = () => new Promise(done => setImmediate(done));

    // The card's rows: each one's class, its text and its buttons.
    const look = () => card.querySelectorAll('.src').map(row => ({
      cls:row.className, id:row.dataset.id || null, source:row.dataset.source || null, text:row.textContent,
      buttons:row.querySelectorAll('button').map(b => [b.textContent, b.className]),
    }));
    const modal = () => {
      const bg = document.body.children[document.body.children.length-1];
      return bg.className==='modal-bg' ? bg : null;
    };
    const lookModal = bg => {
      const box = bg.children[0];
      return {
        title:box.querySelector('h4').textContent, hint:box.querySelector('[data-f="hint"]').textContent,
        what:box.querySelector('[data-f="what"]').textContent,
        buttons:box.querySelectorAll('button').map(b => [b.dataset.act, b.textContent, b.className]),
        msg:box.querySelector('[data-f="msg"]').textContent,
      };
    };
    // Remove data clicked, then confirmed: what the page sent, what it
    // said, and the card once the refresh has landed.
    async function removeVia(button){
      button.onclick();
      const bg = modal();
      const out = {modal:lookModal(bg)};
      const confirm = bg.children[0].querySelector('[data-act="confirm"]');
      await confirm.onclick();
      await settle();
      out.calls = calls.splice(0);
      out.loads = loads.splice(0);
      out.closed = !bg.parent;
      out.after = bg.parent ? lookModal(bg) : null;
      out.confirm = [confirm.textContent, !!confirm.disabled];
      out.card = look();
      return out;
    }
    // An orphan row's metrics, opened: each line's metric, name and button.
    const metricLines = () => card.querySelectorAll('.orphan-metric').map(l => [
      l.dataset.metric, l.querySelector('[data-f="metric"]').textContent,
      l.querySelector('button').textContent, l.querySelector('button').className]);
    const openOrphan = () => card.querySelector('.src.orphan .orphan-head').onclick();
"""


def _orphan_script(page, before, after, reply, scenario):
    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)
    harness = (
        ORPHAN_HARNESS.replace("BEFORE_ = BEFORE", f"BEFORE_ = {json.dumps(before)}")
        .replace("AFTER_ = AFTER", f"AFTER_ = {json.dumps(after)}")
        .replace("REPLY_ = REPLY", f"REPLY_ = {json.dumps(reply)}")
    )
    return (
        PARSING_DOM + esc + "\n" + "\n".join(_js(page, n) for n in ORPHAN_PARTS) + harness
        + "(async () => {\n" + scenario + "\nconsole.log(JSON.stringify(r));\n})()"
        + ".catch(e => { console.error(e); process.exit(1); });"
    )


@pytest.fixture
def orphaned_app(client, db_path, tmp_path, monkeypatch):
    """app:old, whose app is uninstalled, with two weigh-ins and a day's
    steps; app:steps, the installed steps app's, with a reading; Garmin
    beside them."""
    import sqlite3

    import main

    path = tmp_path / "apps"
    path.mkdir()
    shutil.copytree(
        os.path.join(os.path.dirname(__file__), "fixtures", "apps", "steps"), path / "steps",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) VALUES (?, ?, ?, ?, ?, 'x')",
        [
            ("2026-10-01", "app:old", "weight_kg", 80.0, "kg"),
            ("2026-10-02", "app:old", "weight_kg", 81.0, "kg"),
            ("2026-10-02", "app:old", "steps", 400, "count"),
            ("2026-10-01", "app:steps", "steps", 500, "count"),
            ("2026-10-02", "garmin", "weight_kg", 82.0, "kg"),
        ],
    )
    conn.commit()
    recompute_derived(conn)
    conn.close()
    return path


def _sent(resp):
    """A reply as the page's send() hands it on: {ok, ...body}."""
    return {"ok": 200 <= resp.status_code < 300, **resp.get_json()}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_an_orphan_shows_remove_data_and_a_live_app_doesnt(client, orphaned_app):
    before = client.get("/api/app-sources").get_json()
    assert [(a["id"], a["orphaned"]) for a in before] == [("old", True), ("steps", False)]
    script = _orphan_script(_page(client), before, before, {"ok": True}, """
        paintSources();
        r.loading = look();
        await loadAppSources();
        r.card = look();
        // Under a Your data row's source list: one line, for the orphan.
        const lines = box => box.querySelectorAll('.orphan-src').map(l => [
          l.querySelector('.dnote').textContent, l.querySelector('button').textContent,
          l.querySelector('button').className]);
        state.roles = {weight_kg:{sources:['garmin', 'app:old', 'app:steps', 'apple_health']}};
        const metricBox = mk('div');
        fillSources({id:'metric:weight_kg', key:'weight_kg', cov:'metrics'}, metricBox);
        r.metric = lines(metricBox);
        const seriesBox = mk('div');
        fillSources({id:'series:heart_rate', key:'heart_rate', cov:'series', grain:'series',
                     sources:['garmin', 'app:steps']}, seriesBox);
        r.series = lines(seriesBox);
        // Its button opens the same confirm, for that metric.
        metricBox.querySelector('.orphan-src button').onclick();
        r.fromRow = lookModal(modal());
        // The orphan's row opens on the metrics it left behind, and shuts.
        const head = card.querySelector('.src.orphan .orphan-head');
        openOrphan();
        r.opened = {expanded:head.getAttribute('aria-expanded'), controls:head.getAttribute('aria-controls'),
                    panel:card.querySelector('.orphan-metrics').id, lines:metricLines()};
        openOrphan();
        r.shut = {expanded:head.getAttribute('aria-expanded'), lines:metricLines()};
    """)
    result = _node(script)

    # Connected sources: the device's row, then the orphan's - name, its
    # source, status and a red Remove all data, its head a button that
    # opens it - and nothing for steps, live.
    assert result["card"] == [
        {"cls": "src", "id": "garmin", "source": None, "text": "", "buttons": []},
        {"cls": "src orphan", "id": None, "source": "app:old",
         "text": "›oldapp:old○ app uninstalledRemove all data",
         "buttons": [["›oldapp:old", "src-name orphan-head"], ["Remove all data", "btn sm danger"]]},
    ]
    assert result["metric"] == [
        ["app:old is from an uninstalled app.", "Remove data", "btn sm danger"],
    ]
    assert result["series"] == []
    assert result["fromRow"]["title"] == "Weight"
    assert result["fromRow"]["what"] == "Remove Weight from the uninstalled app 'old'? This can't be undone."
    # Opened: a line per metric, in the listing's order, each named and
    # removable on its own.
    assert result["opened"] == {
        "expanded": "true", "controls": "orphan-app-old", "panel": "orphan-app-old",
        "lines": [["steps", "Steps", "Remove data", "btn sm danger"],
                  ["weight_kg", "Weight", "Remove data", "btn sm danger"]],
    }
    assert result["shut"] == {"expanded": "false", "lines": []}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_remove_data_confirms_what_goes_then_calls_the_route_and_refreshes(client, orphaned_app):
    import sqlite3

    import main

    before = client.get("/api/app-sources").get_json()
    # The request the page is checked below to send, made here first, so
    # the listing it refreshes to is the server's own.
    reply = _sent(client.delete("/api/app-sources/old"))
    after = client.get("/api/app-sources").get_json()
    assert reply == {"ok": True, "removed": {"metrics": 3, "sessions": 0, "samples": 0}}
    assert [a["id"] for a in after] == ["steps"]
    conn = sqlite3.connect(main.app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE source = 'app:old'").fetchone()[0] == 0
    conn.close()

    script = _orphan_script(_page(client), before, after, reply, """
        await loadAppSources();
        Object.assign(r, await removeVia(card.querySelector('.src.orphan [data-act="remove-data"]')));
    """)
    result = _node(script)

    # The confirm names what goes, as text.
    assert result["modal"] == {
        "title": "app:old",
        "hint": "Its app is no longer installed. What it wrote is still in your data.",
        "what": "Remove all data from the uninstalled app 'old'? 3 readings across 2 metrics (Steps, Weight). "
                "This can't be undone.",
        "buttons": [["cancel", "Cancel", "btn sm"], ["confirm", "Remove data", "btn sm danger solid"]],
        "msg": "",
    }
    # Confirmed: the route, once, for old; the popup closes, the data card
    # and Home are re-read, and the card no longer has the orphan.
    assert result["calls"] == ["old"]
    assert result["closed"] is True
    assert result["loads"] == ["data", "home"]
    assert [row["cls"] for row in result["card"]] == ["src"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
@pytest.mark.parametrize("url, click, call", [
    # Remove all data, and one metric's Remove data in the opened row.
    ("/api/app-sources/old", "card.querySelector('.src.orphan [data-act=\"remove-data\"]')", "old"),
    ("/api/app-sources/old/steps",
     "(openOrphan(), card.querySelector('.orphan-metric [data-act=\"remove-metric\"]'))", ["old", "steps"]),
], ids=["all", "metric"])
def test_a_refused_removal_says_why_and_keeps_the_source(client, orphaned_app, url, click, call):
    before = client.get("/api/app-sources").get_json()
    # Reinstalled since the page read the listing: the server refuses.
    old = orphaned_app / "old"
    old.mkdir()
    (old / "app.yaml").write_text("id: old\nname: Old\nsizes: [{w: 1, h: 1}]\n")
    reply = _sent(client.delete(url))
    assert reply["ok"] is False and "is present" in reply["error"]

    script = _orphan_script(_page(client), before, before, reply, f"""
        await loadAppSources();
        Object.assign(r, await removeVia({click}));
    """)
    result = _node(script)

    assert result["calls"] == [call]
    assert result["closed"] is False
    assert result["after"]["msg"] == reply["error"]
    assert result["confirm"] == ["Remove data", False]
    assert result["loads"] == []
    assert [row["source"] for row in result["card"]] == [None, "app:old"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_metrics_remove_data_in_the_orphan_row_takes_only_that_metric(client, orphaned_app):
    import sqlite3

    import main

    before = client.get("/api/app-sources").get_json()
    reply = _sent(client.delete("/api/app-sources/old/steps"))
    after = client.get("/api/app-sources").get_json()
    assert reply == {"ok": True, "removed": {"metrics": 1, "sessions": 0, "samples": 0}}
    assert [(a["id"], a["metrics"]) for a in after] == [("old", ["weight_kg"]), ("steps", ["steps"])]
    conn = sqlite3.connect(main.app.config["DB_PATH"])
    assert conn.execute(
        "SELECT metric, COUNT(*) FROM metrics WHERE source = 'app:old' GROUP BY metric"
    ).fetchall() == [("weight_kg", 2)]
    conn.close()

    script = _orphan_script(_page(client), before, after, reply, """
        await loadAppSources();
        openOrphan();
        const steps = card.querySelectorAll('.orphan-metric').find(l => l.dataset.metric==='steps');
        Object.assign(r, await removeVia(steps.querySelector('[data-act="remove-metric"]')));
        r.lines = metricLines();
    """)
    result = _node(script)

    # The confirm names the metric and the app, as text.
    assert result["modal"] == {
        "title": "Steps",
        "hint": "From app:old, whose app is no longer installed. The rest of what it wrote stays.",
        "what": "Remove Steps from the uninstalled app 'old'? This can't be undone.",
        "buttons": [["cancel", "Cancel", "btn sm"], ["confirm", "Remove data", "btn sm danger solid"]],
        "msg": "",
    }
    # The metric route, once, for old's steps; the same refresh as Remove
    # all data - and the orphan stays, still open, on what's left.
    assert result["calls"] == [["old", "steps"]]
    assert result["closed"] is True
    assert result["loads"] == ["data", "home"]
    assert [row["source"] for row in result["card"]] == [None, "app:old"]
    assert result["lines"] == [["weight_kg", "Weight", "Remove data", "btn sm danger"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_your_datas_remove_data_takes_only_that_rows_metric(client, orphaned_app):
    before = client.get("/api/app-sources").get_json()
    roles = client.get("/api/source-roles").get_json()
    assert "app:old" in roles["weight_kg"]["sources"]
    reply = _sent(client.delete("/api/app-sources/old/weight_kg"))
    after = client.get("/api/app-sources").get_json()
    assert reply == {"ok": True, "removed": {"metrics": 2, "sessions": 0, "samples": 0}}
    # Garmin's weigh-in, and old's steps, are still there.
    assert client.get("/api/source-roles").get_json()["weight_kg"]["sources"] == ["garmin"]
    assert [(a["id"], a["metrics"]) for a in after] == [("old", ["steps"]), ("steps", ["steps"])]

    script = _orphan_script(_page(client), before, after, reply, f"""
        await loadAppSources();
        state.roles = {json.dumps(roles)};
        const box = mk('div');
        fillSources({{id:'metric:weight_kg', key:'weight_kg', cov:'metrics'}}, box);
        Object.assign(r, await removeVia(box.querySelector('.orphan-src [data-act="remove-data"]')));
    """)
    result = _node(script)

    assert result["modal"]["title"] == "Weight"
    assert result["modal"]["what"] == "Remove Weight from the uninstalled app 'old'? This can't be undone."
    assert result["calls"] == [["old", "weight_kg"]]
    assert result["closed"] is True
    assert result["loads"] == ["data", "home"]
    # The app's steps are left, so its row stays.
    assert [row["source"] for row in result["card"]] == [None, "app:old"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_an_orphan_with_no_readings_left_says_remove_all_data_takes_the_rest(client, orphaned_app):
    import sqlite3

    import main

    conn = sqlite3.connect(main.app.config["DB_PATH"])
    conn.execute(
        "INSERT INTO samples (source, metric, ts, value, unit, synced_at) "
        "VALUES ('app:z', 'spo2', '2026-10-02T03:00:00Z', 96, '%', 'x')"
    )
    conn.commit()
    conn.close()
    listing = [a for a in client.get("/api/app-sources").get_json() if a["id"] == "z"]
    assert listing[0]["metrics"] == [] and listing[0]["samples"] == 1

    script = _orphan_script(_page(client), listing, listing, {"ok": True}, """
        await loadAppSources();
        openOrphan();
        r.note = card.querySelector('.orphan-item .dnote').textContent;
        r.lines = metricLines();
    """)
    result = _node(script)

    assert result["note"] == "No readings left - 1 sample, which Remove all data takes."
    assert result["lines"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_summary_counts_every_kind_of_row(client):
    page = _page(client)
    script = "\n".join(_js(page, n) for n in ["num", "countOf", "appDataSummary"]) + """
        const metricName = k => ({weight_kg:'Weight', steps:'Steps'})[k] || k;
        console.log(JSON.stringify([
          appDataSummary({metrics:['weight_kg'], readings:1, sessions:0, samples:0}),
          appDataSummary({metrics:['steps', 'weight_kg'], readings:1200, sessions:1, samples:3}),
          appDataSummary({metrics:[], readings:0, sessions:2, samples:0}),
        ]));
    """
    assert _node(script) == [
        "1 reading across 1 metric (Weight)",
        "1,200 readings across 2 metrics (Steps, Weight), 1 session and 3 samples",
        "2 sessions",
    ]


# ---------- an app's own metric in Your data ----------
#
# The catalog lists an installed app's own metrics after the canonical
# ones, each under app:<id>:<key> and marked with its `app` and `label`.
# Your data names one by its label, marks it as the app's, and - with no
# source role for it (/api/source-roles lists the canonical metrics) -
# lists its source from the catalog, not as a priority to drag.


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_your_data_names_an_apps_own_metric_and_lists_its_source(client, tmp_path, monkeypatch):
    import main

    path = tmp_path / "apps"
    shutil.copytree(
        os.path.join(os.path.dirname(__file__), "fixtures", "apps", "caffeine"), path / "caffeine",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    declared = client.get("/api/catalog?include_empty=1").get_json()
    resp = client.post("/api/app/caffeine/action", json={"action": "log", "payload": {"caffeine_mg": 95}})
    assert resp.status_code == 200
    logged = client.get("/api/catalog?include_empty=1").get_json()
    roles = client.get("/api/source-roles").get_json()
    assert "app:caffeine:caffeine_mg" not in roles

    page = _page(client)
    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)
    multiline = [re.search(rf"\n  (const {n} = .*?;)\n", page, re.S).group(1) for n in ("titleCase", "metricName")]
    parts = [_js(page, n) for n in ("$", "el", "METRIC_NAMES", "CUSTOM_LABELS", "buildRows",
                                    "orphans", "orphanLines", "fillSources", "sourceList")]
    script = PARSING_DOM + esc + "\n" + "\n".join(parts[:4] + multiline + parts[4:]) + f"""
        const srcName = id => id;
        const makeSortable = () => {{ throw new Error('an app metric is not sortable'); }};
        const state = {{appSources:[], roles:{json.dumps(roles)}, note:{{}}}};
        const look = catalog => {{
          const row = buildRows(catalog).find(r => r.key === 'app:caffeine:caffeine_mg');
          const box = mk('div');
          fillSources(row, box);
          return {{
            name:metricName(row.key), app:row.app, category:row.category, grain:row.grain,
            tiles:box.querySelectorAll('.stile').map(t => [t.dataset.src, t.textContent]),
            notes:box.querySelectorAll('.dnote').map(n => n.textContent),
          }};
        }};
        console.log(JSON.stringify({{
          declared:look({json.dumps(declared)}), logged:look({json.dumps(logged)}),
          // Its app gone, an orphan's metric still reads: by its short key.
          gone:metricName('app:old:theanine_mg'), canonical:metricName('weight_kg'),
        }}));
    """
    result = _node(script)

    assert result["declared"] == {
        "name": "Caffeine", "app": "caffeine", "category": "Nutrition & hydration", "grain": "daily",
        "tiles": [], "notes": ["Defined by the app 'caffeine'. Nothing logged yet."],
    }
    assert result["logged"] == {
        "name": "Caffeine", "app": "caffeine", "category": "Nutrition & hydration", "grain": "daily",
        "tiles": [["app:caffeine", "app:caffeinePRIMARY"]],
        "notes": ["Defined by the app 'caffeine': only it writes this metric."],
    }
    assert (result["gone"], result["canonical"]) == ("Theanine Mg", "Weight")
    # The row marks it as the app's beside its name, all as escaped text.
    row = _js_function(page, "rowItem")
    assert '<span class="d-app">app</span>' in row
    assert "${esc(metricName(row.key))}" in row and "data-tip=\"${esc(tip)}\"" in row


def test_remove_data_reaches_the_route_through_the_api_block(client):
    page = _page(client)
    # The only place the URLs live.
    assert "    appSources:      ()          => fetchJSON('/api/app-sources')," in page
    assert "    removeAppSource: id          => send('/api/app-sources/'+enc(id), 'DELETE')," in page
    assert ("    removeAppSourceMetric: (id, metric) => "
            "send('/api/app-sources/'+enc(id)+'/'+enc(metric), 'DELETE'),") in page
    confirm = _js_function(page, "openRemoveAppData")
    # One confirm, two routes: no metric is the whole app; a metric, just it.
    assert "const whole = metric==null;" in confirm
    assert "whole ? await api.removeAppSource(o.id) : await api.removeAppSourceMetric(o.id, metric)" in confirm
    # Either way, the same refresh: the sources, the data card and Home.
    assert confirm.count("loadAppSources();") == 1 and "if(res.ok){" in confirm
    ok = confirm[confirm.index("if(res.ok){"):confirm.index("}else{")]
    assert all(call in ok for call in ("loadAppSources();", "loadData();", "loadHome();"))
    for value in ("fetch(", "'/api/"):
        assert value not in confirm, value
    # Each entry point hands the confirm what it's for: the whole app, the
    # opened row's metric, Your data's row's.
    assert "openRemoveAppData(o, null)" in _js_function(page, "orphanRow")
    assert "openRemoveAppData(o, key)" in _js_function(page, "orphanMetrics")
    assert "openRemoveAppData(o, row.key)" in _js_function(page, "orphanLines")
    assert "orphanLines(row, sources, box);" in _js_function(page, "fillSources")
    # Every id, source, metric and count is set as text: the markup itself
    # is fixed, nothing interpolated into it.
    for name, call in (("openRemoveAppData", "openModal("), ("orphanRow", "el('div','src-r', "),
                       ("orphanRow", "el('button','src-name orphan-head', "),
                       ("orphanMetrics", "el('li','orphan-metric', "),
                       ("orphanLines", "el('div','orphan-src', ")):
        body = _js_function(page, name)
        markup = re.search(re.escape(call) + r"`(.*?)`", body, re.S)
        assert markup, name
        assert "${" not in markup.group(1), name
        assert "innerHTML" not in body, name
    # Read at boot, and again whenever what's orphaned may change: an
    # uninstall, an install - and after a removal, above.
    assert "  refreshConnectors();\n  loadAppSources();\n" in page
    assert "loadAppSources();   // what it wrote stays - orphaned now" in _js_function(page, "appPanel")
    assert "loadAppSources();" in _js_function(page, "appInstalled")


def test_the_pages_script_compiles(client):
    if shutil.which("node") is None:
        pytest.skip("needs node to compile the page's script")
    # The whole of it, as served: a stray character (a raw line separator
    # in a regex, say) would stop every part of the page.
    page = _page(client)
    [script] = re.findall(r"<script>(.*?)</script>", page, re.S)
    out = subprocess.run(
        ["node", "-e", "new (require('vm').Script)(require('fs').readFileSync(0, 'utf8'))"],
        input=script, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr


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
