"""
TZ1: the profile - the account's own settings, the same on every device
it signs in from - and its first field, the timezone.

GET /api/profile is {timezone, timezone_set, zones}: the zone in effect
(UTC while none is saved), whether one is, and every IANA name it can
be. PUT /api/profile {timezone} saves one of those, else 400 with
nothing stored. The settings row behind it, profile.timezone, is refused
through the generic /api/settings/<key> both ways, so nothing skips the
check. Then the settings popup's Your account section, run under node:
the zone or "UTC (not set)", "Use this device's timezone" only where it
differs, Save sending PUT /api/profile and reloading on a change, and
the opening page still this browser's alone.

What the timezone does - every today following it - is test_clock.py's.
"""

import json
import re
import shutil

import pytest

import main
from clock import TIMEZONE_KEY, zones

H = {"X-OpenFit": "1"}


def stored(client):
    conn = main.get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (TIMEZONE_KEY,)).fetchone()
    conn.close()
    return row["value"] if row else None


# ---------- the routes ----------


def test_get_shows_the_default_then_the_saved_zone(client):
    first = client.get("/api/profile").get_json()
    assert (first["timezone"], first["timezone_set"]) == ("UTC", False)
    assert first["zones"] == list(zones()) and "Asia/Hong_Kong" in first["zones"]

    resp = client.put("/api/profile", json={"timezone": "Asia/Hong_Kong"})
    assert (resp.status_code, resp.get_json()) == (200, {"ok": True, "timezone": "Asia/Hong_Kong"})

    after = client.get("/api/profile").get_json()
    assert (after["timezone"], after["timezone_set"]) == ("Asia/Hong_Kong", True)
    assert stored(client) == "Asia/Hong_Kong"


def test_put_replaces_and_utc_is_a_zone_like_any(client):
    client.put("/api/profile", json={"timezone": "Europe/London"})
    client.put("/api/profile", json={"timezone": "UTC"})

    got = client.get("/api/profile").get_json()
    assert (got["timezone"], got["timezone_set"]) == ("UTC", True)


@pytest.mark.parametrize("body", [
    {"timezone": "Mars/Olympus_Mons"},
    {"timezone": ""},
    {"timezone": " Asia/Hong_Kong"},
    {"timezone": "asia/hong_kong"},
    {"timezone": "localtime"},
    {"timezone": "../../etc/passwd"},
    {"timezone": None},
    {"timezone": 8},
    {"timezone": True},
    {"timezone": ["Asia/Hong_Kong"]},
    {"timezone": {"name": "Asia/Hong_Kong"}},
    {},
])
def test_put_refuses_anything_but_a_known_zone(client, body):
    client.put("/api/profile", json={"timezone": "Europe/London"})

    resp = client.put("/api/profile", json=body)

    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": main.BAD_TIMEZONE}
    assert stored(client) == "Europe/London"


@pytest.mark.parametrize("raw", ['"Asia/Hong_Kong"', "[]", "null", "nonsense"])
def test_put_refuses_a_body_that_isnt_an_object(client, raw):
    resp = client.put("/api/profile", data=raw, content_type="application/json")

    assert resp.status_code == 400 and resp.get_json()["ok"] is False
    assert stored(client) is None


def test_put_refuses_a_field_the_profile_doesnt_have(client):
    resp = client.put("/api/profile", json={"timezone": "Asia/Hong_Kong", "units": "metric"})

    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": "unknown profile field: units"})
    assert stored(client) is None


def test_get_reads_an_unreadable_stored_zone_as_utc_not_set(client):
    conn = main.get_conn()
    conn.execute("INSERT INTO settings (key, value) VALUES (?, 'Atlantis/Capital')", (TIMEZONE_KEY,))
    conn.commit()
    conn.close()

    got = client.get("/api/profile").get_json()
    assert (got["timezone"], got["timezone_set"]) == ("UTC", False)


@pytest.mark.parametrize("key", ["profile.timezone", "profile.units", "profile."])
def test_the_generic_settings_route_refuses_profile_keys(client, key):
    put = client.put(f"/api/settings/{key}", json={"value": "Not/A_Zone"})
    get = client.get(f"/api/settings/{key}")

    for resp in (put, get):
        assert resp.status_code == 400
        assert resp.get_json() == {"ok": False, "error": main.PROFILE_ONLY}
    assert stored(client) is None
    # The rest of the store is as it was.
    assert client.put("/api/settings/profiles", json={"value": "x"}).status_code == 200
    assert client.get("/api/settings/profiles").get_json()["value"] == "x"


def test_the_profile_is_behind_the_sign_in_and_its_write_behind_the_header(client, db_path, monkeypatch):
    guest = main.app.test_client()
    for resp in (guest.get("/api/profile"),
                 guest.put("/api/profile", json={"timezone": "Asia/Hong_Kong"}, headers=H)):
        assert (resp.status_code, resp.get_json()) == (401, {"ok": False, "error": "signed out"})

    del client.environ_base["HTTP_X_OPENFIT"]
    resp = client.put("/api/profile", json={"timezone": "Asia/Hong_Kong"})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "This request didn't come from OpenFit's page."
    assert stored(client) is None
    assert client.put("/api/profile", json={"timezone": "Asia/Hong_Kong"}, headers=H).status_code == 200
    # A read needs no header.
    assert client.get("/api/profile").get_json()["timezone"] == "Asia/Hong_Kong"


# ---------- the settings popup ----------
#
# openSettings() as the page has it, run under node over test_routes.py's
# stand-in DOM - which parses what reaches innerHTML, so text set as
# markup would show up as elements - with the real openModal(), a
# stand-in api, localStorage and location, and the browser's own zone
# whatever the test says.

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")

SETTINGS_PARTS = ["$", "el", "openModal", "OPENING_TAB", "tabButtons", "openingTab",
                  "deviceZone", "TZ_UNSET", "openSettings"]

SETTINGS_HARNESS = r"""
    const r = {};
    const Intl = {DateTimeFormat: () => ({resolvedOptions: () => ({timeZone: DEVICE})})};
    const home = {layout: null};
    const store = {};
    const localStorage = {getItem: k => k in store ? store[k] : null, setItem: (k, v) => { store[k] = String(v); }};
    const location = {reloads: 0, reload(){ this.reloads++; }};
    const puts = [];
    const served = PROFILE;
    const api = {
      profile: () => served===null ? Promise.reject(new Error('500')) : Promise.resolve(JSON.parse(JSON.stringify(served))),
      saveProfile: async body => { puts.push(body); return REPLY; },
      signOut: async () => ({ok:true}),
    };
    // The strip, as far as the opening page goes: Home and Data.
    for(const [tab, text] of [['home', 'Home'], ['data', 'Data']]){
      const b = document.body.appendChild(mk('button'));
      b.className = 'tab'; b.dataset.tab = tab; b.textContent = text;
    }
    const tick = () => new Promise(done => setTimeout(done, 0));
    let box = null;
    const open = async () => {
      openSettings();
      await tick();
      box = document.body.children.find(n => n.className==='modal-bg').children[0];
      // As a browser starts a select the markup drew: on its selected option.
      const tabs = box.querySelector('#openingTab');
      if(tabs.value===undefined){
        const on = tabs.children.find(o => 'selected' in o.attrs) || tabs.children[0];
        tabs.value = on.attrs.value;
      }
      return box;
    };
    const q = sel => box.querySelector(sel);
    const shown = () => {
      const zone = q('#tzZone'), device = q('[data-act="device-tz"]');
      return {
        heads: box.querySelectorAll('.settings-h').map(h => h.textContent),
        labels: box.querySelectorAll('label').map(l => l.textContent),
        hint: box.querySelectorAll('.settings-sec')[1].querySelector('.hint').textContent,
        value: zone.value,
        first: zone.children.length ? [zone.children[0].value, zone.children[0].textContent] : null,
        options: zone.children.length,
        disabled: zone.disabled===undefined ? 'disabled' in zone.attrs : zone.disabled,
        device: device.hidden ? null : device.textContent,
        msg: q('[data-f="tz-msg"]').textContent,
        open: document.body.children.some(n => n.className==='modal-bg'),
      };
    };
"""


def _settings(client, scenario, profile, device="Asia/Hong_Kong", reply=None):
    from test_routes import PARSING_DOM, _js, _node, _page

    page = _page(client)
    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)
    script = (PARSING_DOM + esc + "\n" + "\n".join(_js(page, n) for n in SETTINGS_PARTS)
              + SETTINGS_HARNESS
                .replace("DEVICE", json.dumps(device))
                .replace("PROFILE", json.dumps(profile))
                .replace("REPLY", json.dumps(reply or {"ok": True}))
              + "\n(async () => {\n" + scenario
              + "\nconsole.log(JSON.stringify(r));\n})().catch(e => { console.error(e); process.exit(1); });")
    return _node(script)


ZONES = ["America/New_York", "Asia/Hong_Kong", "Asia/Tokyo", "Europe/London", "UTC"]


def profile(saved=None, zones=ZONES):
    return {"timezone": saved or "UTC", "timezone_set": saved is not None, "zones": zones}


@needs_node
def test_your_account_shows_utc_not_set_and_offers_the_devices_zone(client):
    r = _settings(client, "await open(); r.s = shown();", profile())

    assert r["s"] == {
        "heads": ["This browser", "Your account"],
        "labels": ["Opening page", "Timezone"],
        "hint": "Used for 'today' everywhere in OpenFit and its apps, on every device you sign in from.",
        "value": "", "first": ["", "UTC (not set)"], "options": len(ZONES) + 1, "disabled": False,
        "device": "Use this device's timezone (Asia/Hong_Kong)", "msg": "", "open": True,
    }


@needs_node
def test_your_account_shows_the_saved_zone(client):
    r = _settings(client, "await open(); r.s = shown();", profile("Europe/London"))

    s = r["s"]
    assert (s["value"], s["first"], s["options"]) == ("Europe/London", ["America/New_York", "America/New_York"], 5)
    assert s["device"] == "Use this device's timezone (Asia/Hong_Kong)"


@needs_node
@pytest.mark.parametrize("saved, device", [
    ("Asia/Hong_Kong", "Asia/Hong_Kong"),   # the same
    ("Europe/London", "Mars/Base"),          # a zone this OpenFit doesn't know
    ("Europe/London", None),                 # the browser won't say
])
def test_the_devices_zone_is_offered_only_where_it_differs(client, saved, device):
    r = _settings(client, "await open(); r.s = shown();", profile(saved), device=device)

    assert r["s"]["device"] is None and r["s"]["value"] == saved


@needs_node
def test_using_the_devices_zone_picks_it_and_save_puts_it_then_reloads(client):
    r = _settings(client, """
        await open();
        q('#openingTab').value = 'home';
        q('[data-act="device-tz"]').onclick();
        r.picked = shown();
        await q('[data-act="save"]').onclick();
        r.puts = puts; r.reloads = location.reloads; r.store = store; r.after = shown();
    """, profile("Europe/London"))

    assert (r["picked"]["value"], r["picked"]["device"]) == ("Asia/Hong_Kong", None)
    assert r["puts"] == [{"timezone": "Asia/Hong_Kong"}]
    assert r["reloads"] == 1
    # The opening page, this browser's alone, saved beside it.
    assert r["store"] == {"of-opening-tab": "home"}


@needs_node
def test_save_with_the_zone_unchanged_saves_only_the_opening_page(client):
    r = _settings(client, """
        await open();
        q('#openingTab').value = 'home';
        await q('[data-act="save"]').onclick();
        r.puts = puts; r.reloads = location.reloads; r.store = store; r.open = shown().open;
    """, profile("Europe/London"))

    assert r == {"puts": [], "reloads": 0, "store": {"of-opening-tab": "home"}, "open": False}


@needs_node
def test_save_while_unset_and_untouched_sets_nothing(client):
    r = _settings(client, """
        await open();
        await q('[data-act="save"]').onclick();
        r.puts = puts; r.reloads = location.reloads; r.store = store;
    """, profile())

    assert r == {"puts": [], "reloads": 0, "store": {"of-opening-tab": "data"}}


@needs_node
def test_a_refused_save_stays_in_the_popup(client):
    r = _settings(client, """
        await open();
        const zone = q('#tzZone');
        zone.value = 'Asia/Tokyo'; zone.onchange();
        await q('[data-act="save"]').onclick();
        r.puts = puts; r.reloads = location.reloads; r.s = shown();
        r.saveOff = q('[data-act="save"]').disabled;
    """, profile("Europe/London"), reply={"ok": False, "error": "timezone must be <b>an</b> IANA name"})

    assert r["puts"] == [{"timezone": "Asia/Tokyo"}] and r["reloads"] == 0
    assert (r["s"]["open"], r["s"]["msg"], r["s"]["value"]) == (True, "timezone must be <b>an</b> IANA name", "Asia/Tokyo")
    assert r["saveOff"] is False


@needs_node
def test_typing_searches_the_zones_and_picks_the_first_match(client):
    r = _settings(client, """
        await open();
        const search = q('#tzSearch'), zone = q('#tzZone');
        search.value = 'hong kong'; search.oninput();
        r.hk = [zone.value, zone.children.map(o => o.value)];
        search.value = 'asia'; search.oninput();
        r.asia = [zone.value, zone.children.map(o => o.value)];
        search.value = 'zzz'; search.oninput();
        r.none = [zone.value, zone.children.map(o => o.value)];
        search.value = ''; search.oninput();
        r.all = [zone.value, zone.children.length];
    """, profile())

    # While none is saved, UTC (not set) stays first; the pick stays listed.
    assert r["hk"] == ["Asia/Hong_Kong", ["", "Asia/Hong_Kong"]]
    assert r["asia"] == ["Asia/Hong_Kong", ["", "Asia/Hong_Kong", "Asia/Tokyo"]]
    assert r["none"] == ["Asia/Hong_Kong", ["", "Asia/Hong_Kong"]]
    assert r["all"] == ["Asia/Hong_Kong", len(ZONES) + 1]


@needs_node
def test_zone_names_are_text_never_markup(client):
    odd = "Etc/<img src=x onerror=alert(1)>"
    r = _settings(client, """
        await open();
        const zone = q('#tzZone');
        r.texts = zone.children.map(o => o.textContent);
        r.markup = below(box).filter(n => n.tag==='img').length;
        r.device = shown().device;
    """, profile(odd, zones=[odd, "UTC"]), device="UTC")

    assert r["texts"] == [odd, "UTC"] and r["markup"] == 0
    assert r["device"] == "Use this device's timezone (UTC)"


@needs_node
def test_a_profile_that_wont_read_still_saves_the_opening_page(client):
    r = _settings(client, """
        await open();
        r.s = shown();
        q('#openingTab').value = 'home';
        await q('[data-act="save"]').onclick();
        r.puts = puts; r.store = store; r.open = shown().open;
    """, None)

    assert (r["s"]["msg"], r["s"]["options"], r["s"]["disabled"], r["s"]["device"]) == (
        "Couldn't read your timezone.", 0, True, None)
    assert r["puts"] == [] and r["store"] == {"of-opening-tab": "home"} and r["open"] is False


def test_the_page_reads_and_writes_the_profile_through_its_api(client):
    from test_routes import _page

    page = _page(client)
    assert "    profile:      ()             => fetchJSON('/api/profile')," in page
    assert "    saveProfile:  body           => send('/api/profile', 'PUT', body)," in page
