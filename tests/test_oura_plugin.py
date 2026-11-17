"""
The Oura plugin against a stand-in for Oura's API v2, with the network
stubbed: what lands where (daily metrics, sleep and workout sessions,
heart-rate samples), what is deliberately not filed, and the token
handling Oura's single-use refresh tokens force.

The token rules are the ones that cost a connection when they slip. A
refresh spends the stored refresh token, so the rotated one must be on
disk - committed - before anything else can fail; it must never land on
an account row that changed while the sync ran (a re-authorisation, a
removal); and a stored access token with time left is reused, so a sync
doesn't rotate at all most of the time.
"""

import json
import sqlite3
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import pytest

from crypto import decrypt, encrypt
from metrics import (
    ACTIVE_KCAL,
    DISTANCE_M,
    HRV_MS,
    INTENSITY_MODERATE_MIN,
    INTENSITY_VIGOROUS_MIN,
    RESPIRATORY_RATE_BRPM,
    RESTING_HR_BPM,
    SEDENTARY_MIN,
    SKIN_TEMPERATURE,
    SKIN_TEMPERATURE_DEVIATION,
    SPO2_PCT,
    STEPS,
    TOTAL_KCAL,
    VO2_MAX,
)
from plugins import PLUGINS
from plugins.base import OAUTH_REDIRECT_URI
from plugins.oura.plugin import MAX_SERIES_DAYS, SCOPES, OuraPlugin

TODAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
CLIENT = {"client_id": "oura-client", "client_secret": "oura-secret"}


# ---------- a stand-in for Oura ----------


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
        self.text = json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"{self.status_code} error")


class FakeOura:
    """Oura's token endpoint and usercollection routes.

    Refresh tokens are single-use, as Oura's are: each refresh retires the
    one sent and issues the next. Collections answer from `docs` with
    every document whatever its day (the plugin applies the window), a
    page of `page_size` at a time. heartrate answers from `hr` rows within
    the asked start/end.
    """

    def __init__(self):
        self.docs = {}
        self.hr = []
        self.page_size = None
        self.fail = set()
        self.live_refresh = {"RT0"}
        self.live_access = {"AT0"}
        self.issued = 0
        self.expires_in = 2_592_000
        self.token_posts = []
        self.gets = []

    def post(self, url, data=None, headers=None, timeout=None):
        assert url == "https://api.ouraring.com/oauth/token"
        assert headers and headers.get("User-Agent")
        self.token_posts.append(dict(data))
        if data["grant_type"] == "refresh_token":
            if data["refresh_token"] not in self.live_refresh:
                return FakeResponse({"error": "invalid_grant"}, 400)
            self.live_refresh.discard(data["refresh_token"])
        self.issued += 1
        refresh, access = f"RT{self.issued}", f"AT{self.issued}"
        self.live_refresh.add(refresh)
        self.live_access.add(access)
        return FakeResponse({"access_token": access, "refresh_token": refresh,
                             "expires_in": self.expires_in, "token_type": "bearer"})

    def get(self, url, headers=None, params=None, timeout=None):
        assert headers.get("User-Agent"), "Oura refuses a request without a User-Agent"
        path = url.rsplit("/", 1)[1]
        self.gets.append((path, dict(params or {})))
        if headers["Authorization"].removeprefix("Bearer ") not in self.live_access:
            return FakeResponse({"detail": "unauthorized"}, 401)
        if path in self.fail:
            return FakeResponse({"detail": "boom"}, 500)
        if path == "heartrate":
            lo = datetime.fromisoformat(params["start_datetime"])
            hi = datetime.fromisoformat(params["end_datetime"])
            rows = [r for r in self.hr
                    if lo <= datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) <= hi]
        else:
            rows = list(self.docs.get(path, []))
        if not self.page_size:
            return FakeResponse({"data": rows, "next_token": None})
        at = int(params.get("next_token") or 0)
        page = rows[at:at + self.page_size]
        more = at + self.page_size < len(rows)
        return FakeResponse({"data": page, "next_token": str(at + self.page_size) if more else None})

    def kill_access(self):
        """Oura stops honouring every access token issued so far."""
        self.live_access.clear()


def store(conn, credentials):
    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(plugin_id) DO UPDATE SET credentials=excluded.credentials",
        ("oura", encrypt(json.dumps(credentials)), "2026-11-01T00:00:00Z"),
    )
    conn.commit()


def stored(conn):
    row = conn.execute("SELECT credentials FROM accounts WHERE plugin_id = 'oura'").fetchone()
    return json.loads(decrypt(row[0])) if row else None


@pytest.fixture
def oura(conn, monkeypatch, pin_clock):
    pin_clock(NOW)
    fake = FakeOura()
    store(conn, {**CLIENT, "refresh_token": "RT0", "access_token": "AT0",
                 "access_token_expires_at": NOW.timestamp() + 10 * 86400})
    monkeypatch.setattr("plugins.oura.plugin.requests.post", fake.post)
    monkeypatch.setattr("plugins.oura.plugin.requests.get", fake.get)
    return fake


def sync(conn, days=7):
    return OuraPlugin().sync(conn, days)


def readings(conn, metric):
    return {
        r["date"]: r["value"]
        for r in conn.execute(
            "SELECT date, value FROM metrics WHERE source = 'oura' AND metric = ?", (metric,)
        )
    }


def sessions(conn, kind):
    return [
        (r["start"], r["end"], json.loads(r["summary_json"]))
        for r in conn.execute(
            'SELECT "start", "end", summary_json FROM sessions WHERE source = \'oura\' '
            "AND kind = ? ORDER BY \"start\"", (kind,)
        )
    ]


def activity(day, **fields):
    return {"id": f"a-{day}", "day": day, "class_5_min": "0003344551" * 20, **fields}


def night(day, type_="long_sleep", **fields):
    doc = {
        "id": f"s-{day}-{type_}", "day": day, "type": type_,
        "bedtime_start": f"{day}T00:10:00+08:00", "bedtime_end": f"{day}T00:40:00+08:00",
        "total_sleep_duration": 1500, "light_sleep_duration": 600,
        "deep_sleep_duration": 540, "rem_sleep_duration": 360, "awake_time": 300,
        "latency": 420, "lowest_heart_rate": 48, "average_hrv": 61,
        "average_breath": 14.25, "sleep_phase_5_min": "411224",
    }
    doc.update(fields)
    return doc


# ---------- registration and the add flow ----------


def test_oura_is_a_registered_oauth_connector():
    plugin = PLUGINS["oura"]
    assert isinstance(plugin, OuraPlugin)
    assert plugin.add_flow == "oauth"
    assert [f["key"] for f in plugin.fields] == ["client_id", "client_secret"]
    assert {f["key"]: f["type"] for f in plugin.fields}["client_secret"] == "password"
    # Oura takes plain http only for "localhost": its own redirect, named
    # in the setup note, not the shared 127.0.0.1 one.
    assert plugin.oauth_redirect_uri == "http://localhost:9109/"
    assert plugin.oauth_redirect_uri != OAUTH_REDIRECT_URI
    assert plugin.oauth_redirect_uri in plugin.add_note
    assert "127.0.0.1" not in plugin.add_note


def test_the_consent_url_asks_for_the_scopes_read_and_carries_pkce():
    url = OuraPlugin().oauth_auth_url(CLIENT, "http://localhost:9109/", "challenge-123")
    assert url.startswith("https://cloud.ouraring.com/oauth/authorize?")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["oura-client"]
    assert params["redirect_uri"] == ["http://localhost:9109/"]
    assert params["scope"] == [" ".join(SCOPES)]
    assert params["code_challenge"] == ["challenge-123"]
    assert params["code_challenge_method"] == ["S256"]
    # Nothing the plugin doesn't read, and never the secret in a URL.
    assert not {"email", "personal"} & set(SCOPES)
    assert "oura-secret" not in url


def test_adding_through_the_routes_stores_both_tokens(client, db_path, monkeypatch, pin_clock):
    pin_clock(NOW)
    fake = FakeOura()
    monkeypatch.setattr("plugins.oura.plugin.requests.post", fake.post)

    start = client.post("/api/connectors/oura/oauth/start", json=CLIENT)
    assert start.status_code == 200
    # The consent URL and the exchange both carry Oura's own redirect.
    consent = urllib.parse.parse_qs(urllib.parse.urlsplit(start.get_json()["auth_url"]).query)
    assert consent["redirect_uri"] == ["http://localhost:9109/"]
    resp = client.post("/api/connectors/oura/oauth/finish",
                       json={"code": "http://localhost:9109/?code=the-code&scope=daily"})
    assert resp.get_json() == {"ok": True}

    exchange = fake.token_posts[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "the-code"
    assert exchange["code_verifier"]
    assert exchange["redirect_uri"] == "http://localhost:9109/"

    conn = sqlite3.connect(db_path)
    creds = stored(conn)
    conn.close()
    assert creds["refresh_token"] == "RT1"
    assert creds["access_token"] == "AT1"
    assert creds["access_token_expires_at"] == NOW.timestamp() + fake.expires_in
    # The account's login is the client id - never a token or the secret.
    detail = client.get("/api/connectors/oura/detail").get_json()
    assert detail["login"] == "oura-client"


# ---------- tokens ----------


def test_a_stored_access_token_with_time_left_is_reused(conn, oura):
    sync(conn)
    assert oura.token_posts == []
    assert stored(conn)["refresh_token"] == "RT0"


def test_a_token_near_expiry_is_refreshed_and_the_rotation_stored(conn, oura):
    creds = stored(conn)
    store(conn, {**creds, "access_token_expires_at": NOW.timestamp() + 60})

    sync(conn)

    assert [p["grant_type"] for p in oura.token_posts] == ["refresh_token"]
    assert oura.token_posts[0]["refresh_token"] == "RT0"
    now = stored(conn)
    assert now["refresh_token"] == "RT1" and now["access_token"] == "AT1"
    assert now["client_id"] == "oura-client" and now["client_secret"] == "oura-secret"
    # The old one is spent at Oura: a sync that kept it would be dead.
    assert "RT0" not in oura.live_refresh


def test_the_rotated_token_survives_a_sync_that_fails_afterwards(conn, oura, monkeypatch, db_path):
    creds = stored(conn)
    store(conn, {**creds, "access_token_expires_at": None})
    oura.docs["daily_activity"] = [activity("2026-11-16", steps=1000)]

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("plugins.oura.plugin.write_metric", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        sync(conn)
    conn.rollback()

    # Read from a connection of its own: what is on disk, not in a
    # transaction the failed sync left open.
    other = sqlite3.connect(db_path)
    try:
        assert stored(other)["refresh_token"] == "RT1"
    finally:
        other.close()


def test_a_401_refreshes_once_and_asks_again(conn, oura):
    oura.kill_access()
    oura.docs["daily_activity"] = [activity("2026-11-16", steps=4321)]

    sync(conn)

    assert len(oura.token_posts) == 1
    assert readings(conn, STEPS) == {"2026-11-16": 4321}
    assert stored(conn)["refresh_token"] == "RT1"


def test_a_401_after_refreshing_fails_the_sync(conn, oura, monkeypatch):
    original = oura.post

    def issue_dud(*args, **kwargs):
        resp = original(*args, **kwargs)
        oura.kill_access()
        return resp

    monkeypatch.setattr("plugins.oura.plugin.requests.post", issue_dud)
    oura.kill_access()
    with pytest.raises(RuntimeError, match="401"):
        sync(conn)


def test_a_refused_refresh_fails_the_sync_loudly(conn, oura):
    creds = stored(conn)
    store(conn, {**creds, "refresh_token": "stale", "access_token_expires_at": 0})
    with pytest.raises(RuntimeError, match="Re-authenticate"):
        sync(conn)
    assert oura.gets == []


def test_no_account_is_not_authorised(conn, oura):
    conn.execute("DELETE FROM accounts")
    conn.commit()
    with pytest.raises(RuntimeError, match="not authorised"):
        sync(conn)


def test_a_reauthorisation_mid_sync_is_never_overwritten(conn, oura, monkeypatch, db_path):
    creds = stored(conn)
    store(conn, {**creds, "access_token_expires_at": 0})
    reauthorised = {**CLIENT, "refresh_token": "FRESH", "access_token": "FRESH-AT",
                    "access_token_expires_at": NOW.timestamp() + 86400}
    original = oura.post

    def reauth_lands_meanwhile(*args, **kwargs):
        resp = original(*args, **kwargs)
        other = sqlite3.connect(db_path)
        store(other, reauthorised)
        other.close()
        return resp

    monkeypatch.setattr("plugins.oura.plugin.requests.post", reauth_lands_meanwhile)
    with pytest.raises(RuntimeError, match="changed while this sync ran"):
        sync(conn)
    assert stored(conn) == reauthorised


def test_replace_credentials_compares_and_never_inserts(conn):
    plugin = OuraPlugin()
    first = {**CLIENT, "refresh_token": "A"}
    assert plugin.replace_credentials(conn, first, {**first, "refresh_token": "B"}) is False
    assert stored(conn) is None

    store(conn, first)
    assert plugin.replace_credentials(conn, {**first, "refresh_token": "X"}, {"x": 1}) is False
    assert stored(conn) == first
    assert plugin.replace_credentials(conn, first, {**first, "refresh_token": "B"}) is True
    assert stored(conn)["refresh_token"] == "B"


def test_replace_credentials_keeps_created_at(conn):
    plugin = OuraPlugin()
    store(conn, {"refresh_token": "A"})
    plugin.replace_credentials(conn, {"refresh_token": "A"}, {"refresh_token": "B"})
    assert conn.execute("SELECT created_at FROM accounts").fetchone()[0] == "2026-11-01T00:00:00Z"


# ---------- daily activity ----------


def test_daily_activity_lands_on_the_canonical_keys(conn, oura):
    oura.docs["daily_activity"] = [activity(
        "2026-11-16", steps=9012, active_calories=432, total_calories=2611,
        sedentary_time=31_500, medium_activity_time=2_430, high_activity_time=610,
        low_activity_time=9_000, equivalent_walking_distance=7_900, score=81,
    )]
    assert sync(conn) == 1
    day = "2026-11-16"
    assert readings(conn, STEPS) == {day: 9012}
    assert readings(conn, ACTIVE_KCAL) == {day: 432}
    assert readings(conn, TOTAL_KCAL) == {day: 2611}
    assert readings(conn, SEDENTARY_MIN) == {day: 525}
    assert readings(conn, INTENSITY_MODERATE_MIN) == {day: 40}  # 40.5 min, rounded
    assert readings(conn, INTENSITY_VIGOROUS_MIN) == {day: 10}
    # Walking-equivalent is not distance covered (law 3); the score is a
    # vendor composite (law 5).
    assert readings(conn, DISTANCE_M) == {}
    metrics = {r[0] for r in conn.execute("SELECT DISTINCT metric FROM metrics")}
    assert metrics == {STEPS, ACTIVE_KCAL, TOTAL_KCAL, SEDENTARY_MIN,
                       INTENSITY_MODERATE_MIN, INTENSITY_VIGOROUS_MIN}


@pytest.mark.parametrize("unworn", [
    {"class_5_min": "0" * 288},
    {"class_5_min": None, "non_wear_time": 86_400},
])
def test_a_day_the_ring_was_not_worn_files_no_activity(conn, oura, unworn):
    oura.docs["daily_activity"] = [
        activity("2026-11-15", steps=0, active_calories=0, total_calories=1650,
                 sedentary_time=0, **unworn),
        activity("2026-11-16", steps=50, total_calories=1700, class_5_min="0" * 287 + "2"),
    ]
    sync(conn)
    assert readings(conn, STEPS) == {"2026-11-16": 50}
    assert readings(conn, TOTAL_KCAL) == {"2026-11-16": 1700}


def test_documents_dated_outside_the_window_are_dropped(conn, oura):
    oura.docs["daily_activity"] = [
        activity("2026-11-10", steps=1), activity("2026-11-11", steps=2),
        activity("2026-11-17", steps=3), activity("2026-11-18", steps=4),
    ]
    sync(conn, days=7)
    assert readings(conn, STEPS) == {"2026-11-11": 2, "2026-11-17": 3}
    asked = dict(oura.gets)["daily_activity"]
    assert asked["start_date"] == "2026-11-11"
    assert asked["end_date"] == "2026-11-18"


def test_every_page_is_read(conn, oura):
    oura.page_size = 2
    oura.docs["daily_activity"] = [activity(f"2026-11-1{i}", steps=100 + i) for i in range(1, 8)]
    sync(conn)
    assert readings(conn, STEPS) == {f"2026-11-1{i}": 100 + i for i in range(1, 8)}
    tokens = [p.get("next_token") for path, p in oura.gets if path == "daily_activity"]
    assert tokens == [None, "2", "4", "6"]


# ---------- sleep ----------


def test_each_sleep_period_is_a_session_with_its_stage_timeline(conn, oura):
    oura.docs["sleep"] = [night("2026-11-16")]
    sync(conn)
    [(start, end, summary)] = sessions(conn, "sleep")
    assert (start, end) == ("2026-11-15T16:10:00Z", "2026-11-15T16:40:00Z")
    assert summary == {
        "asleep_minutes": 25, "light_minutes": 10, "deep_minutes": 9, "rem_minutes": 6,
        "awake_minutes": 5, "latency_minutes": 7, "main_sleep": True,
        "utc_offset_minutes": 480,
        # "411224": runs merged, the last block cut at bedtime_end.
        "stages": [
            {"start": "2026-11-15T16:10:00Z", "end": "2026-11-15T16:15:00Z", "stage": "awake"},
            {"start": "2026-11-15T16:15:00Z", "end": "2026-11-15T16:25:00Z", "stage": "deep"},
            {"start": "2026-11-15T16:25:00Z", "end": "2026-11-15T16:35:00Z", "stage": "light"},
            {"start": "2026-11-15T16:35:00Z", "end": "2026-11-15T16:40:00Z", "stage": "awake"},
        ],
    }


def test_naps_are_sessions_but_rest_and_deleted_periods_are_not(conn, oura):
    nap = night("2026-11-16", "late_nap", bedtime_start="2026-11-16T15:00:00+08:00",
                bedtime_end="2026-11-16T15:30:00+08:00")
    short = night("2026-11-15", "sleep", bedtime_start="2026-11-15T13:00:00+08:00",
                  bedtime_end="2026-11-15T13:20:00+08:00")
    rest = night("2026-11-14", "rest", bedtime_start="2026-11-14T13:00:00+08:00")
    deleted = night("2026-11-13", "deleted")
    oura.docs["sleep"] = [nap, short, rest, deleted]
    sync(conn)
    got = sessions(conn, "sleep")
    assert [s[0] for s in got] == ["2026-11-15T05:00:00Z", "2026-11-16T07:00:00Z"]
    assert all(s[2]["main_sleep"] is False for s in got)


def test_the_nights_vitals_come_from_its_long_sleep(conn, oura):
    oura.docs["sleep"] = [
        night("2026-11-16", lowest_heart_rate=47, average_hrv=58, average_breath=14.5),
        # A nap the same day doesn't speak for the night.
        night("2026-11-16", "late_nap", bedtime_start="2026-11-16T15:00:00+08:00",
              lowest_heart_rate=60, average_hrv=20, average_breath=17),
        # Of two long sleeps on one day, the longer is the night.
        night("2026-11-15", total_sleep_duration=3_000, lowest_heart_rate=50,
              bedtime_start="2026-11-14T23:00:00+08:00"),
        night("2026-11-15", total_sleep_duration=24_000, lowest_heart_rate=45,
              bedtime_start="2026-11-15T01:00:00+08:00"),
        # A rest period never does.
        night("2026-11-14", "rest", lowest_heart_rate=40),
    ]
    sync(conn)
    assert readings(conn, RESTING_HR_BPM) == {"2026-11-16": 47, "2026-11-15": 45}
    assert readings(conn, HRV_MS)["2026-11-16"] == 58
    assert readings(conn, RESPIRATORY_RATE_BRPM)["2026-11-16"] == 14.5


# ---------- the other daily types ----------


def test_spo2_temperature_deviation_and_vo2_max(conn, oura):
    oura.docs["daily_spo2"] = [
        {"day": "2026-11-16", "spo2_percentage": {"average": 96.8}},
        {"day": "2026-11-15", "spo2_percentage": None},
        # Oura's zero is no reading: below the plausible range, not filed.
        {"day": "2026-11-14", "spo2_percentage": {"average": 0}},
    ]
    oura.docs["daily_readiness"] = [
        {"day": "2026-11-16", "temperature_deviation": -0.31, "score": 77},
        {"day": "2026-11-15", "temperature_deviation": None},
    ]
    oura.docs["vO2_max"] = [{"day": "2026-11-12", "vo2_max": 44}]
    sync(conn)
    assert readings(conn, SPO2_PCT) == {"2026-11-16": 96.8}
    assert readings(conn, SKIN_TEMPERATURE_DEVIATION) == {"2026-11-16": -0.31}
    # A deviation is never an absolute skin temperature (law 3).
    assert readings(conn, SKIN_TEMPERATURE) == {}
    assert readings(conn, VO2_MAX) == {"2026-11-12": 44}


def test_a_failed_type_costs_only_its_own_data(conn, oura):
    oura.fail = {"daily_readiness", "heartrate"}
    oura.docs["daily_activity"] = [activity("2026-11-16", steps=777)]
    oura.docs["daily_readiness"] = [{"day": "2026-11-16", "temperature_deviation": 0.2}]
    sync(conn)
    assert readings(conn, STEPS) == {"2026-11-16": 777}
    assert readings(conn, SKIN_TEMPERATURE_DEVIATION) == {}


# ---------- workouts ----------


def test_a_workout_is_a_session_with_only_what_oura_reports(conn, oura):
    oura.docs["workout"] = [
        {"id": "w1", "day": "2026-11-16", "activity": "Running", "calories": 412.6,
         "distance": 6_020.5, "intensity": "moderate", "source": "confirmed",
         "start_datetime": "2026-11-16T07:00:00.000+08:00",
         "end_datetime": "2026-11-16T07:40:00.000+08:00"},
        {"id": "w2", "day": "2026-11-16", "activity": "walking", "calories": None,
         "distance": None, "start_datetime": None, "end_datetime": None},
    ]
    sync(conn)
    assert sessions(conn, "workout") == [(
        "2026-11-15T23:00:00Z", "2026-11-15T23:40:00Z",
        {"type": "running", "distance_m": 6020.5, "calories_kcal": 413},
    )]


# ---------- heart rate ----------


def test_heart_rate_samples_are_filed_at_their_own_times(conn, oura):
    oura.hr = [
        {"timestamp": "2026-11-16T03:00:03+00:00", "bpm": 61, "source": "awake"},
        {"timestamp": "2026-11-16T03:05:03.500Z", "bpm": 59.6, "source": "rest"},
        {"timestamp": "2026-11-16T03:10:03+00:00", "bpm": 0, "source": "awake"},
    ]
    sync(conn)
    rows = conn.execute(
        "SELECT ts, value, unit FROM samples WHERE source = 'oura' ORDER BY ts"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-11-16T03:00:03Z", 61, "bpm"), ("2026-11-16T03:05:03Z", 60, "bpm"),
    ]


def test_heart_rate_is_asked_for_in_windows_from_local_midnight_to_now(conn, oura):
    sync(conn, days=30)
    windows = [(datetime.fromisoformat(p["start_datetime"]), datetime.fromisoformat(p["end_datetime"]))
               for path, p in oura.gets if path == "heartrate"]
    first_day = TODAY - timedelta(days=29)
    assert windows[0][0] == datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc)
    assert windows[-1][1] == NOW
    assert all(b - a <= timedelta(days=MAX_SERIES_DAYS) for a, b in windows)
    assert all(windows[i][1] == windows[i + 1][0] for i in range(len(windows) - 1))


def test_a_resync_replaces_rather_than_duplicates(conn, oura):
    oura.docs["daily_activity"] = [activity("2026-11-16", steps=10)]
    oura.docs["sleep"] = [night("2026-11-16")]
    oura.hr = [{"timestamp": "2026-11-16T03:00:00+00:00", "bpm": 61}]
    sync(conn)
    oura.docs["daily_activity"] = [activity("2026-11-16", steps=20)]
    sync(conn)
    assert readings(conn, STEPS) == {"2026-11-16": 20}
    assert len(sessions(conn, "sleep")) == 1
    assert conn.execute("SELECT COUNT(*) FROM samples WHERE source='oura'").fetchone()[0] == 1
