"""
Plugin sync() contract, exercised with a fake in-process plugin.

Nothing here talks to Garmin or Google - the point is the database
contract every SyncPlugin has to honour, not any particular vendor API.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from plugins.base import SyncPlugin, iso_utc, write_metric, write_session
from crypto import encrypt
from metrics import METRICS, SESSION_KINDS, SLEEP, SLEEP_MINUTES, STEPS, WORKOUT


class FakePlugin(SyncPlugin):
    """A minimal SyncPlugin that writes whatever readings it's handed.

    Each row is a date plus any canonical metrics, e.g.
    {"date": "2026-01-01", "steps": 9000, "sleep_minutes": 450}.
    """

    name = "Fake source"
    required_env = []

    def __init__(self, plugin_id, rows):
        self.id = plugin_id
        self.rows = rows

    def sync(self, conn, days: int) -> int:
        written = 0
        for row in self.rows[:days] if days else self.rows:
            for metric in METRICS:
                write_metric(conn, row["date"], self.id, metric, row.get(metric))
            written += 1
        conn.commit()
        return written


def readings_for(conn, source):
    """Every (date, metric, value, unit) a source has, in a stable order."""
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT date, metric, value, unit FROM metrics "
            "WHERE source = ? ORDER BY date, metric",
            (source,),
        )
    ]


def test_sync_writes_rows_and_returns_count(conn):
    plugin = FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}])

    assert plugin.sync(conn, 7) == 1
    assert readings_for(conn, "fake") == [("2026-01-01", "steps", 9000, "count")]


def test_resync_upserts_rather_than_duplicating(conn):
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}]).sync(conn, 7)
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9500}]).sync(conn, 7)

    assert readings_for(conn, "fake") == [("2026-01-01", "steps", 9500, "count")]


def test_partial_sync_keeps_metrics_it_did_not_fetch(conn):
    """A later partial sync must not blank out metrics an earlier one filled."""
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000, "sleep_minutes": 450}]).sync(conn, 7)
    FakePlugin("fake", [{"date": "2026-01-01", "resting_hr_bpm": 52}]).sync(conn, 7)

    assert readings_for(conn, "fake") == [
        ("2026-01-01", "resting_hr_bpm", 52, "bpm"),
        ("2026-01-01", "sleep_minutes", 450, "min"),
        ("2026-01-01", "steps", 9000, "count"),
    ]


def test_two_sources_same_date_do_not_contend(conn):
    """The regression the per-source key fixes.

    Under the old date-only primary key the second sync overwrote the
    first source's row for the day; now each source keeps its own.
    """
    day = "2026-01-01"
    FakePlugin("garmin", [{"date": day, "steps": 9000, "resting_hr_bpm": 52}]).sync(conn, 7)
    FakePlugin("google_health", [{"date": day, "steps": 8000, "sleep_minutes": 390}]).sync(conn, 7)

    assert readings_for(conn, "garmin") == [
        (day, "resting_hr_bpm", 52, "bpm"),
        (day, "steps", 9000, "count"),
    ]
    assert readings_for(conn, "google_health") == [
        (day, "sleep_minutes", 390, "min"),
        (day, "steps", 8000, "count"),
    ]

    total = conn.execute("SELECT COUNT(*) FROM metrics WHERE date = ?", (day,)).fetchone()[0]
    assert total == 4


def test_sources_stay_independent_across_repeated_syncs(conn):
    day = "2026-01-01"
    garmin = FakePlugin("garmin", [{"date": day, "steps": 9000}])
    google = FakePlugin("google_health", [{"date": day, "steps": 8000}])

    for _ in range(3):
        garmin.sync(conn, 7)
        google.sync(conn, 7)

    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 2
    assert readings_for(conn, "garmin") == [(day, "steps", 9000, "count")]


def test_write_metric_skips_a_missing_value(conn):
    assert write_metric(conn, "2026-01-01", "fake", STEPS, None) is False
    assert write_metric(conn, "2026-01-01", "fake", STEPS, 0) is True

    assert readings_for(conn, "fake") == [("2026-01-01", "steps", 0, "count")]


def test_write_metric_rejects_a_metric_outside_the_vocabulary(conn):
    with pytest.raises(ValueError, match="unknown metric"):
        write_metric(conn, "2026-01-01", "fake", "stepz", 9000)

    assert readings_for(conn, "fake") == []


def test_resync_refreshes_synced_at(conn):
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}]).sync(conn, 7)
    conn.execute("UPDATE metrics SET synced_at = '2000-01-01 00:00:00'")
    conn.commit()

    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}]).sync(conn, 7)

    assert conn.execute("SELECT synced_at FROM metrics").fetchone()[0] > "2000-01-01 00:00:00"


# ---------- write_session: interval records beside the metrics ----------


NIGHT = {"asleep_minutes": 443, "light_minutes": 255, "deep_minutes": 91,
         "rem_minutes": 97, "awake_minutes": 27}


def sessions_for(conn, source):
    """Every session a source has, summary decoded, in start order."""
    return [
        (r["id"], r["kind"], r["start"], r["end"], json.loads(r["summary_json"]))
        for r in conn.execute(
            'SELECT id, kind, "start", "end", summary_json FROM sessions '
            'WHERE source = ? ORDER BY "start"',
            (source,),
        )
    ]


def test_write_session_writes_a_row(conn):
    wrote = write_session(conn, "fake", "sleep", "2026-01-01T22:30:00Z",
                          "2026-01-02T06:20:00Z", NIGHT)
    conn.commit()

    assert wrote is True
    row = conn.execute('SELECT id, source, kind, "start", "end", synced_at FROM sessions').fetchone()
    assert tuple(row)[:5] == (
        "fake:sleep:2026-01-01T22:30:00Z", "fake", "sleep",
        "2026-01-01T22:30:00Z", "2026-01-02T06:20:00Z",
    )
    assert row["synced_at"]


def test_write_session_summary_round_trips_as_json(conn):
    write_session(conn, "fake", "sleep", "2026-01-01T22:30:00Z", "2026-01-02T06:20:00Z", NIGHT)

    stored = conn.execute("SELECT summary_json FROM sessions").fetchone()[0]
    assert isinstance(stored, str)
    assert json.loads(stored) == NIGHT


def test_rewriting_a_session_upserts_rather_than_duplicating(conn):
    """A re-sync of the same night replaces its end and summary in place."""
    start = "2026-01-01T22:30:00Z"
    write_session(conn, "fake", "sleep", start, "2026-01-02T05:00:00Z", {"asleep_minutes": 380})
    conn.execute("UPDATE sessions SET synced_at = '2000-01-01 00:00:00'")
    write_session(conn, "fake", "sleep", start, "2026-01-02T06:20:00Z", NIGHT)

    assert sessions_for(conn, "fake") == [
        (f"fake:sleep:{start}", "sleep", start, "2026-01-02T06:20:00Z", NIGHT)
    ]
    assert conn.execute("SELECT synced_at FROM sessions").fetchone()[0] > "2000-01-01 00:00:00"


def test_sessions_are_keyed_by_source_kind_and_start(conn):
    """Two sources' sessions for the same night never contend."""
    start = "2026-01-01T22:30:00Z"
    write_session(conn, "garmin", "sleep", start, None, {"asleep_minutes": 443})
    write_session(conn, "google_health", "sleep", start, None, {"asleep_minutes": 421})
    write_session(conn, "garmin", "sleep", "2026-01-02T22:45:00Z", None, {"asleep_minutes": 410})

    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3
    assert [s[4] for s in sessions_for(conn, "google_health")] == [{"asleep_minutes": 421}]


def test_the_session_kinds_are_sleep_and_workout():
    assert SESSION_KINDS == {SLEEP, WORKOUT} == {"sleep", "workout"}


@pytest.mark.parametrize("kind", [SLEEP, WORKOUT])
def test_write_session_accepts_each_session_kind(conn, kind):
    start = "2026-01-01T22:30:00Z"
    assert write_session(conn, "fake", kind, start, None, {}) is True

    assert sessions_for(conn, "fake") == [(f"fake:{kind}:{start}", kind, start, None, {})]


def test_write_session_rejects_a_kind_outside_the_vocabulary(conn):
    with pytest.raises(ValueError, match="unknown session kind"):
        write_session(conn, "fake", "sleeep", "2026-01-01T22:30:00Z", None, NIGHT)

    assert sessions_for(conn, "fake") == []


def test_an_unknown_kind_fails_even_without_a_start(conn):
    """A typo shows on the first call, not only once a session has a start."""
    with pytest.raises(ValueError, match="unknown session kind"):
        write_session(conn, "fake", "run", None, None, {})


def test_write_session_skips_a_missing_start(conn):
    assert write_session(conn, "fake", "sleep", None, "2026-01-02T06:20:00Z", NIGHT) is False

    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_write_session_leaves_metrics_alone(conn):
    """Dual-track: a session is written beside the daily total, never into it."""
    write_metric(conn, "2026-01-02", "fake", SLEEP_MINUTES, 443)
    write_session(conn, "fake", "sleep", "2026-01-01T22:30:00Z", "2026-01-02T06:20:00Z", NIGHT)

    assert readings_for(conn, "fake") == [("2026-01-02", "sleep_minutes", 443, "min")]


def test_iso_utc_is_one_shape_for_every_source():
    """Whole seconds, always UTC, Z suffix - so starts compare as strings."""
    moment = datetime(2026, 1, 1, 23, 30, 0, 999_000, tzinfo=timezone(timedelta(hours=1)))

    assert iso_utc(moment) == "2026-01-01T22:30:00Z"
    assert iso_utc(datetime(2026, 1, 1, 22, 30)) == "2026-01-01T22:30:00Z"  # naive = UTC


def test_real_plugins_implement_the_interface():
    """The registry's plugins must still be SyncPlugins (no network here)."""
    from plugins import PLUGINS

    assert PLUGINS
    for plugin_id, plugin in PLUGINS.items():
        assert isinstance(plugin, SyncPlugin)
        assert plugin.id == plugin_id
        assert plugin.name
        status = plugin.status()
        assert set(status) >= {"configured", "missing_env"}


def test_sync_plugin_cannot_be_instantiated_without_sync():
    class Incomplete(SyncPlugin):
        id = "incomplete"
        name = "Incomplete"

    with pytest.raises(TypeError):
        Incomplete()


# ---------- credentials: manifest, storage, env fallback ----------


class ManifestPlugin(SyncPlugin):
    """A plugin that declares a connector manifest and syncs what it reads.

    Stands in for a real device: the point is the credential plumbing, not
    any particular vendor's API.
    """

    id = "fake_device"
    name = "Fake Device"
    add_flow = "credentials"
    fields = [
        {"key": "username", "label": "Username", "type": "text",
         "required": True, "env": "FAKE_DEVICE_USER"},
        {"key": "token", "label": "API token", "type": "password",
         "required": True, "env": "FAKE_DEVICE_TOKEN"},
        {"key": "nickname", "label": "Nickname", "type": "text", "required": False},
    ]

    def sync(self, conn, days: int) -> int:
        credentials = self.get_credentials(conn)
        if not credentials.get("username") or not credentials.get("token"):
            raise RuntimeError("not connected")
        write_metric(conn, "2026-01-01", self.id, STEPS, 1234)
        conn.commit()
        return 1


def store_account(conn, plugin_id, credentials):
    """Write an accounts row the way POST /api/connectors would."""
    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(plugin_id) DO UPDATE SET credentials=excluded.credentials",
        (plugin_id, encrypt(json.dumps(credentials)), "2026-01-01T00:00:00Z"),
    )
    conn.commit()


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("FAKE_DEVICE_USER", "FAKE_DEVICE_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_plugin_reads_its_credentials_from_the_database(conn, clean_env):
    plugin = ManifestPlugin()
    store_account(conn, plugin.id, {"username": "sam", "token": "t0ken"})

    assert plugin.get_credentials(conn) == {"username": "sam", "token": "t0ken"}


def test_stored_credentials_reach_sync(conn, clean_env):
    """The whole point: adding the device in the UI makes sync() work."""
    plugin = ManifestPlugin()

    with pytest.raises(RuntimeError):
        plugin.sync(conn, 7)

    store_account(conn, plugin.id, {"username": "sam", "token": "t0ken"})

    assert plugin.sync(conn, 7) == 1
    assert readings_for(conn, plugin.id) == [("2026-01-01", "steps", 1234, "count")]


def test_credentials_fall_back_to_the_env_when_there_is_no_account(conn, monkeypatch):
    """A pre-UI install keeps working until the device is re-added."""
    monkeypatch.setenv("FAKE_DEVICE_USER", "sam")
    monkeypatch.setenv("FAKE_DEVICE_TOKEN", "t0ken")

    assert ManifestPlugin().get_credentials(conn) == {
        "username": "sam",
        "token": "t0ken",
    }


def test_a_stored_account_takes_precedence_over_the_env(conn, monkeypatch):
    monkeypatch.setenv("FAKE_DEVICE_USER", "from-env")
    monkeypatch.setenv("FAKE_DEVICE_TOKEN", "env-token")
    plugin = ManifestPlugin()
    store_account(conn, plugin.id, {"username": "from-db", "token": "db-token"})

    assert plugin.get_credentials(conn)["username"] == "from-db"
    assert plugin.get_credentials(conn)["token"] == "db-token"


def test_the_env_still_fills_a_field_the_account_predates(conn, monkeypatch):
    """A field added to the manifest later isn't in an older stored blob."""
    monkeypatch.setenv("FAKE_DEVICE_TOKEN", "env-token")
    plugin = ManifestPlugin()
    store_account(conn, plugin.id, {"username": "sam"})

    assert plugin.get_credentials(conn) == {"username": "sam", "token": "env-token"}


def test_status_is_driven_by_the_manifest(conn, clean_env):
    plugin = ManifestPlugin()

    assert plugin.status(conn) == {
        "configured": False,
        "missing_env": ["FAKE_DEVICE_USER", "FAKE_DEVICE_TOKEN"],
    }

    store_account(conn, plugin.id, {"username": "sam", "token": "t0ken"})

    assert plugin.status(conn) == {"configured": True, "missing_env": []}


def test_optional_fields_do_not_block_configured(conn, clean_env):
    plugin = ManifestPlugin()
    store_account(conn, plugin.id, {"username": "sam", "token": "t0ken"})

    # `nickname` is required=False, so leaving it out is fine.
    assert plugin.status(conn)["configured"] is True
    assert "nickname" not in plugin.get_credentials(conn)


def test_missing_fields_reports_the_manifest_entries(clean_env):
    plugin = ManifestPlugin()

    missing = plugin.missing_fields({"username": "sam"})

    assert [f["key"] for f in missing] == ["token"]


def test_a_plugin_without_a_manifest_still_uses_required_env(monkeypatch):
    """The old required_env path still works for a manifest-less plugin."""

    class LegacyPlugin(SyncPlugin):
        id = "legacy"
        name = "Legacy"
        required_env = ["LEGACY_TOKEN"]

        def sync(self, conn, days: int) -> int:
            return 0

    monkeypatch.delenv("LEGACY_TOKEN", raising=False)
    assert LegacyPlugin().status() == {
        "configured": False,
        "missing_env": ["LEGACY_TOKEN"],
    }

    monkeypatch.setenv("LEGACY_TOKEN", "x")
    assert LegacyPlugin().status()["configured"] is True


def test_clear_cached_auth_defaults_to_a_no_op(conn):
    """A plugin that caches nothing needn't implement it."""
    assert ManifestPlugin().clear_cached_auth() is None


def test_real_plugins_declare_a_usable_manifest():
    """Whatever a plugin declares, the UI has to be able to render it."""
    from plugins import PLUGINS

    for plugin in PLUGINS.values():
        assert plugin.add_flow in ("credentials", "oauth")
        for field in plugin.fields:
            assert field["key"]
            assert field["label"]
            assert field["type"] in ("text", "password")
            assert isinstance(field["required"], bool)
