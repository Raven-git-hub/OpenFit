"""
Settings key/value API - the store behind the persisted tile layout.

The value is an opaque JSON string: whatever the client PUT is what a
later GET hands back, byte for byte. These tests pin that contract, and
the "unset key returns null" behaviour the frontend relies on to fall
back to its default layout on a fresh install.
"""

import json


def test_setting_round_trip(client):
    layout = json.dumps({"tl": "activity", "tr": "weight", "bl": "today", "br": "adddata"})

    assert client.put("/api/settings/home_tiles", json={"value": layout}).status_code == 200

    assert client.get("/api/settings/home_tiles").get_json() == {
        "key": "home_tiles",
        "value": layout,
    }


def test_unset_key_returns_null_not_404(client):
    """A first run has no saved preference - that is normal, not an error."""
    resp = client.get("/api/settings/home_tiles")

    assert resp.status_code == 200
    assert resp.get_json() == {"key": "home_tiles", "value": None}


def test_put_overwrites_existing_value(client):
    client.put("/api/settings/home_tiles", json={"value": '{"tl":"weight"}'})
    client.put("/api/settings/home_tiles", json={"value": '{"tl":"sleep"}'})

    assert client.get("/api/settings/home_tiles").get_json()["value"] == '{"tl":"sleep"}'


def test_keys_are_independent(client):
    client.put("/api/settings/home_tiles", json={"value": "a"})
    client.put("/api/settings/other", json={"value": "b"})

    assert client.get("/api/settings/home_tiles").get_json()["value"] == "a"
    assert client.get("/api/settings/other").get_json()["value"] == "b"


def test_put_requires_a_string_value(client):
    for body in ({}, {"value": None}, {"value": {"tl": "weight"}}, {"value": 3}):
        resp = client.put("/api/settings/home_tiles", json=body)
        assert resp.status_code == 400, body
        assert "error" in resp.get_json()

    # Nothing was stored by any of the rejected requests.
    assert client.get("/api/settings/home_tiles").get_json()["value"] is None
