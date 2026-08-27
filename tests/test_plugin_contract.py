"""
Plugin sync() contract, exercised with a fake in-process plugin.

Nothing here talks to Garmin or Google - the point is the database
contract every SyncPlugin has to honour, not any particular vendor API.
"""

import json

import pytest

from plugins.base import SyncPlugin
from crypto import encrypt


class FakePlugin(SyncPlugin):
    """A minimal SyncPlugin that writes whatever rows it's handed."""

    name = "Fake source"
    required_env = []

    def __init__(self, plugin_id, rows):
        self.id = plugin_id
        self.rows = rows

    def sync(self, conn, days: int) -> int:
        written = 0
        for row in self.rows[:days] if days else self.rows:
            conn.execute(
                """
                INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date, source) DO UPDATE SET
                    steps=COALESCE(excluded.steps, activity.steps),
                    resting_hr=COALESCE(excluded.resting_hr, activity.resting_hr),
                    sleep_hours=COALESCE(excluded.sleep_hours, activity.sleep_hours),
                    synced_at=datetime('now')
                """,
                (
                    row["date"],
                    row.get("steps"),
                    row.get("resting_hr"),
                    row.get("sleep_hours"),
                    self.id,
                ),
            )
            written += 1
        conn.commit()
        return written


def rows_for(conn, source):
    return conn.execute(
        "SELECT date, steps, resting_hr, sleep_hours FROM activity "
        "WHERE source = ? ORDER BY date",
        (source,),
    ).fetchall()


def test_sync_writes_rows_and_returns_count(conn):
    plugin = FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}])

    assert plugin.sync(conn, 7) == 1
    assert [tuple(r) for r in rows_for(conn, "fake")] == [("2026-01-01", 9000, None, None)]


def test_resync_upserts_rather_than_duplicating(conn):
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}]).sync(conn, 7)
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9500}]).sync(conn, 7)

    assert [tuple(r) for r in rows_for(conn, "fake")] == [("2026-01-01", 9500, None, None)]


def test_upsert_coalesces_missing_fields(conn):
    """A later partial sync must not blank out fields an earlier one filled."""
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000, "sleep_hours": 7.5}]).sync(conn, 7)
    FakePlugin("fake", [{"date": "2026-01-01", "resting_hr": 52}]).sync(conn, 7)

    assert [tuple(r) for r in rows_for(conn, "fake")] == [("2026-01-01", 9000, 52, 7.5)]


def test_two_sources_same_date_do_not_contend(conn):
    """The regression the (date, source) reshape fixes.

    Under the old date-only primary key the second sync overwrote the
    first source's row for the day; now each source keeps its own.
    """
    day = "2026-01-01"
    FakePlugin("garmin", [{"date": day, "steps": 9000, "resting_hr": 52}]).sync(conn, 7)
    FakePlugin("google_health", [{"date": day, "steps": 8000, "sleep_hours": 6.5}]).sync(conn, 7)

    assert [tuple(r) for r in rows_for(conn, "garmin")] == [(day, 9000, 52, None)]
    assert [tuple(r) for r in rows_for(conn, "google_health")] == [(day, 8000, None, 6.5)]

    total = conn.execute("SELECT COUNT(*) FROM activity WHERE date = ?", (day,)).fetchone()[0]
    assert total == 2


def test_sources_stay_independent_across_repeated_syncs(conn):
    day = "2026-01-01"
    garmin = FakePlugin("garmin", [{"date": day, "steps": 9000}])
    google = FakePlugin("google_health", [{"date": day, "steps": 8000}])

    for _ in range(3):
        garmin.sync(conn, 7)
        google.sync(conn, 7)

    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 2
    assert [tuple(r) for r in rows_for(conn, "garmin")] == [(day, 9000, None, None)]


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
        conn.execute(
            "INSERT INTO activity (date, steps, source, synced_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(date, source) DO UPDATE SET steps=excluded.steps",
            ("2026-01-01", 1234, self.id),
        )
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
    assert conn.execute(
        "SELECT steps FROM activity WHERE source = ?", (plugin.id,)
    ).fetchone()[0] == 1234


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
