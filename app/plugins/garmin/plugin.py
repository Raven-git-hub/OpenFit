"""
Garmin Connect sync plugin.

Auth: uses GARMIN_EMAIL / GARMIN_PASSWORD from environment on first login,
then caches a session token on disk (GARMIN_TOKENSTORE) so subsequent syncs
reuse it instead of logging in with the password again. If your account has
MFA enabled, run first_login.py once (see repo README) before starting the
scheduled sync.

Fetched per day: get_stats (steps, resting HR) and get_sleep_data (the
daily sleep total plus the night as a sleep session). Fetched once over
the whole range: get_activities_by_date, each activity a workout session.
"""

import os
import shutil
from datetime import date, datetime, timedelta, timezone

from garminconnect import Garmin, GarminConnectAuthenticationError

from metrics import RESTING_HR_BPM, SLEEP, SLEEP_MINUTES, STEPS, WORKOUT

from ..base import SyncPlugin, iso_utc, write_metric, write_session

# dailySleepDTO stage fields (seconds) -> the sleep session summary keys
# (minutes). A field the device doesn't report - remSleepSeconds on a
# watch without REM tracking - is simply left out of the summary.
SLEEP_STAGE_FIELDS = {
    "lightSleepSeconds": "light_minutes",
    "deepSleepSeconds": "deep_minutes",
    "remSleepSeconds": "rem_minutes",
    "awakeSleepSeconds": "awake_minutes",
}

# Activity fields from get_activities_by_date -> the workout session
# summary keys, each with its conversion to the canonical unit. duration
# is the timer time (pauses excluded), in seconds; distance is already
# metres. A field the activity doesn't carry - distance on a strength
# session, averageHR without a heart-rate sensor - is left out.
WORKOUT_FIELDS = {
    "duration": ("duration_minutes", lambda seconds: round(seconds / 60)),
    "distance": ("distance_m", float),
    "averageHR": ("avg_hr_bpm", round),
    "calories": ("calories_kcal", round),
}


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
            sleep_session = None

            try:
                stats = client.get_stats(d_str)
                steps = stats.get("totalSteps")
                resting_hr = stats.get("restingHeartRate")
            except Exception as e:
                print(f"[garmin] stats fetch failed for {d_str}: {e}")

            try:
                sleep = client.get_sleep_data(d_str)
                dto = sleep.get("dailySleepDTO") or {}
                seconds = dto.get("sleepTimeSeconds")
                if seconds:
                    sleep_minutes = round(seconds / 60)
                    # The same response carries the night as an interval
                    # with its stages. Never raises - a shape mismatch
                    # costs the session, not the metric.
                    sleep_session = _sleep_session(dto, sleep_minutes, d_str)
            except Exception as e:
                print(f"[garmin] sleep fetch failed for {d_str}: {e}")

            if steps is None and resting_hr is None and sleep_minutes is None:
                continue

            # A None (that fetch failed, or Garmin had nothing) writes
            # nothing and leaves any earlier reading in place.
            write_metric(conn, d_str, self.id, STEPS, steps)
            write_metric(conn, d_str, self.id, RESTING_HR_BPM, resting_hr)
            write_metric(conn, d_str, self.id, SLEEP_MINUTES, sleep_minutes)
            if sleep_session:
                write_session(conn, self.id, SLEEP, *sleep_session)
            written += 1

        # Workouts are sessions only - no daily metric - fetched in one go
        # for the whole range rather than a request per day.
        for session in self._fetch_workouts(client, today - timedelta(days=days - 1), today):
            write_session(conn, self.id, WORKOUT, *session)

        conn.commit()
        return written

    def _fetch_workouts(self, client, start, end):
        """Every activity from `start` to `end` (dates, both inclusive) as
        a (start, end, summary) workout session.

        One get_activities_by_date call covers the range: the library
        pages through Garmin's activity search 20 at a time until a page
        comes back empty, and raises rather than loop forever should one
        never do so. A failed fetch is logged and costs the workouts
        only - the day loop's metrics and sleep are already written.
        """
        try:
            activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
        except Exception as e:
            print(f"[garmin] activities fetch failed for {start} to {end}: {e}")
            return []
        sessions = []
        for activity in activities or []:
            session = _workout_session(activity)
            if session:
                sessions.append(session)
        return sessions


def _sleep_session(dto, asleep_minutes, d_str):
    """The night in a dailySleepDTO as a (start, end, summary) session, or None.

    The interval comes from sleepStartTimestampGMT/sleepEndTimestampGMT
    (epoch milliseconds, real UTC - the *Local pair is shifted by the
    timezone and garminconnect warns it can be shifted twice), and each
    stage from its *SleepSeconds field, rounded to whole minutes.
    asleep_minutes is the night's sleep_minutes reading, so the session
    and the daily total always agree.

    Defensive throughout: a missing stage is left out of the summary, a
    missing start means no session (logged), and nothing here raises.
    """
    try:
        start = _gmt_iso(dto.get("sleepStartTimestampGMT"))
        if start is None:
            print(
                f"[garmin] sleep on {d_str} has no usable sleepStartTimestampGMT - "
                f"no session written. dailySleepDTO keys: {sorted(dto)}"
            )
            return None

        summary = {"asleep_minutes": asleep_minutes}
        for field, key in SLEEP_STAGE_FIELDS.items():
            seconds = dto.get(field)
            if isinstance(seconds, (int, float)):
                summary[key] = round(seconds / 60)
        if len(summary) == 1:
            print(
                f"[garmin] sleep on {d_str} has no stage fields - session written "
                f"with asleep_minutes only. dailySleepDTO keys: {sorted(dto)}"
            )

        return start, _gmt_iso(dto.get("sleepEndTimestampGMT")), summary
    except Exception as e:
        print(f"[garmin] could not read the sleep session for {d_str}: {e}")
        return None


def _workout_session(activity):
    """One activity from get_activities_by_date as a (start, end, summary)
    workout session, or None.

    The interval starts at startTimeGMT ("YYYY-MM-DD HH:MM:SS", real UTC
    - startTimeLocal is wall-clock time) and runs for elapsedDuration
    seconds, pauses included, falling back to duration where that's all
    there is. The summary is activityType.typeKey, lowercased, plus each
    field in WORKOUT_FIELDS.

    Defensive throughout: a field that isn't there (or isn't a number) is
    left out of the summary, no duration at all means no end, an
    activity with no usable start is logged and skipped, and nothing here
    raises.
    """
    try:
        start = _gmt_datetime(activity.get("startTimeGMT"))
        if start is None:
            print(
                f"[garmin] activity {activity.get('activityId')} has no usable "
                f"startTimeGMT - no session written. activity keys: {sorted(activity)}"
            )
            return None

        summary = {}
        type_key = (activity.get("activityType") or {}).get("typeKey")
        if isinstance(type_key, str) and type_key:
            summary["type"] = type_key.lower()
        for field, (key, convert) in WORKOUT_FIELDS.items():
            value = activity.get(field)
            if isinstance(value, (int, float)):
                summary[key] = convert(value)

        elapsed = activity.get("elapsedDuration")
        if not isinstance(elapsed, (int, float)):
            elapsed = activity.get("duration")
        end = None
        if isinstance(elapsed, (int, float)):
            end = iso_utc(start + timedelta(seconds=elapsed))
        return iso_utc(start), end, summary
    except Exception as e:
        print(f"[garmin] could not read a workout: {e}")
        return None


def _gmt_datetime(value):
    """Garmin's "YYYY-MM-DD HH:MM:SS" GMT timestamp as a datetime, or None.

    It carries no offset, so it parses naive, and iso_utc() takes a
    naive datetime to be UTC already.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _gmt_iso(millis):
    """Garmin's epoch-milliseconds GMT timestamp as an iso_utc() string."""
    if not isinstance(millis, (int, float)):
        return None
    try:
        return iso_utc(datetime.fromtimestamp(millis / 1000, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None
