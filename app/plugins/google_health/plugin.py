"""
Google Health API sync plugin. Covers data from Pixel Watch and Fitbit
devices, which now flow through Google's newer Health API (health.googleapis.com)
rather than the legacy Fitbit Web API, which Google is deprecating in
September 2026.

Auth is standard Google OAuth 2.0 (not a password): you register an OAuth
client in Google Cloud Console, then run authorize.py ONCE to grant access
and save a refresh token to disk. This plugin just uses that refresh token
to get short-lived access tokens on each sync - see README for full setup.

Data types used (Google Health API v4):
  - steps                     -> dailyRollUp (clean daily sums)
  - sleep                     -> list, summed per civil day from session summaries
  - daily-resting-heart-rate  -> list (no rollup available for this type)
"""

import json
import os
from datetime import date, timedelta

import requests

from ..base import SyncPlugin

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4/users/me"


class GoogleHealthPlugin(SyncPlugin):
    id = "google_health"
    name = "Google Health (Pixel Watch / Fitbit)"
    # Declared now so the connector API can describe this plugin, but the
    # UI deliberately doesn't offer oauth-type connectors yet: adding
    # Google means bouncing through Google's consent screen, which is its
    # own flow (authorize.py, for now) and its own PR.
    add_flow = "oauth"
    fields = [
        {
            "key": "client_id",
            "label": "OAuth client ID",
            "type": "text",
            "required": True,
            "env": "GOOGLE_HEALTH_CLIENT_ID",
        },
        {
            "key": "client_secret",
            "label": "OAuth client secret",
            "type": "password",
            "required": True,
            "env": "GOOGLE_HEALTH_CLIENT_SECRET",
        },
    ]
    required_env = ["GOOGLE_HEALTH_CLIENT_ID", "GOOGLE_HEALTH_CLIENT_SECRET"]

    def _tokenstore_path(self):
        return os.getenv("GOOGLE_HEALTH_TOKENSTORE", "/data/.google_health_token.json")

    def clear_cached_auth(self) -> None:
        """Drop the saved refresh token when the device is removed."""
        path = self._tokenstore_path()
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"[google_health] could not remove {path}: {e}")

    def _load_tokens(self):
        path = self._tokenstore_path()
        if not os.path.exists(path):
            raise RuntimeError(
                "No Google Health token found. Run authorize.py once first "
                "(see README) to grant access."
            )
        with open(path) as f:
            return json.load(f)

    def _get_access_token(self, conn=None):
        tokens = self._load_tokens()
        # Same source of truth as every other plugin: the stored account
        # if there is one, the env vars otherwise.
        creds = self.get_credentials(conn)
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": tokens["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def sync(self, conn, days: int) -> int:
        access_token = self._get_access_token(conn)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        today = date.today()
        start = today - timedelta(days=days - 1)

        steps_by_date = self._fetch_steps(headers, start, today)
        sleep_by_date = self._fetch_sleep(headers, start, today)
        hr_by_date = self._fetch_resting_hr(headers, start, today)

        all_dates = set(steps_by_date) | set(sleep_by_date) | set(hr_by_date)
        written = 0
        for d_str in all_dates:
            conn.execute(
                """
                INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at)
                VALUES (?, ?, ?, ?, 'google_health', datetime('now'))
                ON CONFLICT(date, source) DO UPDATE SET
                    steps=COALESCE(excluded.steps, activity.steps),
                    resting_hr=COALESCE(excluded.resting_hr, activity.resting_hr),
                    sleep_hours=COALESCE(excluded.sleep_hours, activity.sleep_hours),
                    synced_at=datetime('now')
                """,
                (
                    d_str,
                    steps_by_date.get(d_str),
                    hr_by_date.get(d_str),
                    sleep_by_date.get(d_str),
                ),
            )
            written += 1

        conn.commit()
        return written

    # ---- individual data type fetchers ----

    def _fetch_steps(self, headers, start, end):
        """Daily step totals via the dailyRollUp endpoint - clean sums, no
        manual aggregation needed."""
        body = {
            "range": {
                "start": {"date": {"year": start.year, "month": start.month, "day": start.day}},
                "end": {"date": _next_day_dict(end)},
            },
            "windowSizeDays": 1,
        }
        try:
            resp = requests.post(
                f"{API_BASE}/dataTypes/steps/dataPoints:dailyRollUp",
                headers=headers, json=body, timeout=20,
            )
            resp.raise_for_status()
            out = {}
            for point in resp.json().get("rollupDataPoints", []):
                d = point.get("civilStartTime", {}).get("date", {})
                if not d:
                    continue
                d_str = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
                count = point.get("steps", {}).get("countSum")
                if count is not None:
                    out[d_str] = int(count)
            return out
        except Exception as e:
            print(f"[google_health] steps fetch failed: {e}")
            return {}

    def _fetch_sleep(self, headers, start, end):
        """Sleep sessions via the list endpoint, summed per civil day."""
        try:
            resp = requests.get(
                f"{API_BASE}/dataTypes/sleep/dataPoints",
                headers=headers,
                params={"filter": f'sleep.interval.civil_start_time >= "{start.isoformat()}"'},
                timeout=20,
            )
            resp.raise_for_status()
            out = {}
            for point in resp.json().get("dataPoints", []):
                sleep = point.get("sleep", {})
                civil = sleep.get("interval", {}).get("civilStartTime", {}).get("date")
                minutes = sleep.get("summary", {}).get("minutesAsleep")
                if not civil or minutes is None:
                    continue
                d_str = f"{civil['year']:04d}-{civil['month']:02d}-{civil['day']:02d}"
                out[d_str] = out.get(d_str, 0) + round(int(minutes) / 60, 1)
            return out
        except Exception as e:
            print(f"[google_health] sleep fetch failed: {e}")
            return {}

    def _fetch_resting_hr(self, headers, start, end):
        """Daily resting heart rate via the list endpoint.

        NOTE: Google's docs don't publish a full example response for
        daily-resting-heart-rate (only list/reconcile are supported, no
        rollup). This walks the response generically looking for a numeric
        bpm-shaped field. If it comes back empty, print the raw response
        once (see the try/except below) and adjust the key name here -
        it's a couple of line change.
        """
        try:
            resp = requests.get(
                f"{API_BASE}/dataTypes/daily-resting-heart-rate/dataPoints",
                headers=headers,
                params={"filter": f'daily_resting_heart_rate.civil_date >= "{start.isoformat()}"'},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            out = {}
            for point in data.get("dataPoints", []):
                payload = point.get("dailyRestingHeartRate", {})
                civil = payload.get("civilDate") or payload.get("date")
                bpm = None
                for key, val in payload.items():
                    if "heartrate" in key.lower() or key.lower() in ("bpm", "value"):
                        try:
                            bpm = int(val)
                            break
                        except (TypeError, ValueError):
                            continue
                if civil and bpm is not None:
                    d_str = f"{civil['year']:04d}-{civil['month']:02d}-{civil['day']:02d}"
                    out[d_str] = bpm
            if not out and data.get("dataPoints"):
                print(f"[google_health] resting HR: got data but couldn't parse bpm field. "
                      f"Sample point: {data['dataPoints'][0]}")
            return out
        except Exception as e:
            print(f"[google_health] resting HR fetch failed: {e}")
            return {}


def _next_day_dict(d):
    nxt = d + timedelta(days=1)
    return {"year": nxt.year, "month": nxt.month, "day": nxt.day}
