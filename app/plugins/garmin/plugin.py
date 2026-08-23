"""
Garmin Connect sync plugin.

Auth: uses GARMIN_EMAIL / GARMIN_PASSWORD from environment on first login,
then caches a session token on disk (GARMIN_TOKENSTORE) so subsequent syncs
reuse it instead of logging in with the password again. If your account has
MFA enabled, run first_login.py once (see repo README) before starting the
scheduled sync.
"""

import os
from datetime import date, timedelta

from garminconnect import Garmin, GarminConnectAuthenticationError

from ..base import SyncPlugin


class GarminPlugin(SyncPlugin):
    id = "garmin"
    name = "Garmin Connect"
    required_env = ["GARMIN_EMAIL", "GARMIN_PASSWORD"]

    def _get_client(self):
        email = os.getenv("GARMIN_EMAIL")
        password = os.getenv("GARMIN_PASSWORD")
        if not email or not password:
            raise RuntimeError("GARMIN_EMAIL / GARMIN_PASSWORD not set")

        tokenstore = os.getenv("GARMIN_TOKENSTORE", "/data/.garminconnect")
        client = Garmin(email, password)
        try:
            client.login(tokenstore)
        except GarminConnectAuthenticationError as e:
            raise RuntimeError(f"Garmin login failed: {e}")
        return client

    def sync(self, conn, days: int) -> int:
        client = self._get_client()
        written = 0
        today = date.today()

        for i in range(days):
            d = today - timedelta(days=i)
            d_str = d.isoformat()

            steps = None
            resting_hr = None
            sleep_hours = None

            try:
                stats = client.get_stats(d_str)
                steps = stats.get("totalSteps")
                resting_hr = stats.get("restingHeartRate")
            except Exception as e:
                print(f"[garmin] stats fetch failed for {d_str}: {e}")

            try:
                sleep = client.get_sleep_data(d_str)
                seconds = (sleep.get("dailySleepDTO") or {}).get("sleepTimeSeconds")
                if seconds:
                    sleep_hours = round(seconds / 3600, 1)
            except Exception as e:
                print(f"[garmin] sleep fetch failed for {d_str}: {e}")

            if steps is None and resting_hr is None and sleep_hours is None:
                continue

            conn.execute(
                """
                INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at)
                VALUES (?, ?, ?, ?, 'garmin', datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    steps=COALESCE(excluded.steps, activity.steps),
                    resting_hr=COALESCE(excluded.resting_hr, activity.resting_hr),
                    sleep_hours=COALESCE(excluded.sleep_hours, activity.sleep_hours),
                    source='garmin',
                    synced_at=datetime('now')
                """,
                (d_str, steps, resting_hr, sleep_hours),
            )
            written += 1

        conn.commit()
        return written
