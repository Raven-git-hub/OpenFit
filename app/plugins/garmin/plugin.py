"""
Garmin Connect sync plugin.

Auth: uses GARMIN_EMAIL / GARMIN_PASSWORD from environment on first login,
then caches a session token on disk (GARMIN_TOKENSTORE) so subsequent syncs
reuse it instead of logging in with the password again. If your account has
MFA enabled, run first_login.py once (see repo README) before starting the
scheduled sync.
"""

import os
import shutil
from datetime import date, timedelta

from garminconnect import Garmin, GarminConnectAuthenticationError

from metrics import RESTING_HR_BPM, SLEEP_MINUTES, STEPS

from ..base import SyncPlugin, write_metric


class GarminPlugin(SyncPlugin):
    id = "garmin"
    name = "Garmin Connect"
    add_flow = "credentials"
    fields = [
        {
            "key": "email",
            "label": "Email",
            "type": "text",
            "required": True,
            "env": "GARMIN_EMAIL",
        },
        {
            "key": "password",
            "label": "Password",
            "type": "password",
            "required": True,
            "env": "GARMIN_PASSWORD",
        },
    ]
    required_env = ["GARMIN_EMAIL", "GARMIN_PASSWORD"]

    def _tokenstore_path(self):
        return os.getenv("GARMIN_TOKENSTORE", "/data/.garminconnect")

    def clear_cached_auth(self) -> None:
        """Remove the cached Garmin session so removing really disconnects.

        Without this, deleting the account row would leave a working
        token on disk - the device would look gone from the UI while the
        container could still log in.
        """
        path = self._tokenstore_path()
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"[garmin] could not remove {path}: {e}")

    def _get_client(self, conn=None):
        creds = self.get_credentials(conn)
        email = creds.get("email")
        password = creds.get("password")
        if not email or not password:
            raise RuntimeError(
                "Garmin is not connected - add it under "
                "Configuration -> Connected sources"
            )

        client = Garmin(email, password)
        try:
            client.login(self._tokenstore_path())
        except GarminConnectAuthenticationError as e:
            raise RuntimeError(f"Garmin login failed: {e}")
        return client

    def sync(self, conn, days: int) -> int:
        client = self._get_client(conn)
        written = 0
        today = date.today()

        for i in range(days):
            d = today - timedelta(days=i)
            d_str = d.isoformat()

            steps = None
            resting_hr = None
            sleep_minutes = None

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
                    sleep_minutes = round(seconds / 60)
            except Exception as e:
                print(f"[garmin] sleep fetch failed for {d_str}: {e}")

            if steps is None and resting_hr is None and sleep_minutes is None:
                continue

            # A None (that fetch failed, or Garmin had nothing) writes
            # nothing and leaves any earlier reading in place.
            write_metric(conn, d_str, self.id, STEPS, steps)
            write_metric(conn, d_str, self.id, RESTING_HR_BPM, resting_hr)
            write_metric(conn, d_str, self.id, SLEEP_MINUTES, sleep_minutes)
            written += 1

        conn.commit()
        return written
