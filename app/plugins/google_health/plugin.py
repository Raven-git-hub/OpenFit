"""
Google Health API sync plugin. Covers data from Pixel Watch and Fitbit
devices, which now flow through Google's newer Health API (health.googleapis.com)
rather than the legacy Fitbit Web API, which Google is deprecating in
September 2026.

Auth is standard Google OAuth 2.0 (not a password): you register an OAuth
client in Google Cloud Console, then add the device in the UI - client id
and secret, then approve on Google's consent screen and paste back the
code. That one-time consent yields a refresh token, stored encrypted in
the accounts table alongside the client details, which this plugin trades
for a short-lived access token on each sync.

authorize.py does the same thing from a terminal and stays for installs
that were set up that way; its on-disk token file is still read as a
fallback when the stored account has no refresh token of its own.

Data types used (Google Health API v4):
  - steps                     -> dailyRollUp (clean daily sums), at most
                                 MAX_ROLLUP_DAYS per request
  - sleep                     -> list, summed per civil day from session summaries;
                                 each data point is also stored as a sleep session.
                                 Asked for by end date; sleep that began before
                                 the sync window is dropped (see _fetch_sleep)
  - daily-resting-heart-rate  -> list (no rollup available for this type)
  - exercise                  -> list, each data point stored as a workout
                                 session (no daily metric). Asked for by civil
                                 start date; every point that comes back is kept

Every fetch reads all its pages (see _all_pages): Google answers a page at
a time, newest first, and a sleep or exercise page holds at most 25
points, so a backfill longer than that spans several.

Field names - list filters and the fields read back - follow the v4
discovery document (https://health.googleapis.com/$discovery/rest?version=v4).
A filter on a field it doesn't list for that data type gets nothing back,
so a wrong name here costs that data type quietly.
"""

import json
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta

import requests

from metrics import RESTING_HR_BPM, SLEEP, SLEEP_MINUTES, STEPS, WORKOUT

from ..base import SyncPlugin, iso_utc, write_metric, write_session

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4/users/me"

# Sleep stage types in summary.stagesSummary -> the sleep session summary
# keys. STAGES sleep reports LIGHT/DEEP/REM/AWAKE; CLASSIC sleep (naps,
# older trackers) reports ASLEEP/RESTLESS/AWAKE instead, and its ASLEEP is
# already counted in minutesAsleep, so only AWAKE carries over from it.
SLEEP_STAGE_KEYS = {
    "LIGHT": "light_minutes",
    "DEEP": "deep_minutes",
    "REM": "rem_minutes",
    "AWAKE": "awake_minutes",
}

# Google hands back a nextPageToken while more pages remain; this only
# stops one that never runs out. 100 pages of sleep is years of nights.
MAX_PAGES = 100

# The longest range dailyRollUp takes for steps (DailyRollUpDataPointsRequest.
# range in the discovery doc); a longer one is rejected outright.
MAX_ROLLUP_DAYS = 90

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]


class GoogleHealthPlugin(SyncPlugin):
    id = "google_health"
    name = "Google Health (Pixel Watch / Fitbit)"
    # Google is added by consent, not by password: the UI collects the
    # client details below, then walks the two oauth_* methods at the
    # bottom of this class.
    add_flow = "oauth"
    add_note = (
        "Create a Google Cloud project, enable the Google Health API, add "
        "yourself as a test user, make an OAuth client of type Desktop app. "
        "Scopes: activity_and_fitness.readonly, sleep.readonly, "
        "health_metrics_and_measurements.readonly"
    )
    # Google only issues a refresh token on first consent; a repeat
    # authorization of an app you already approved comes back without
    # one until you revoke it.
    oauth_refresh_help = (
        "Google didn't return a refresh token - revoke access at "
        "myaccount.google.com/permissions and try again"
    )
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

    def _legacy_refresh_token(self):
        """The refresh token authorize.py wrote to disk, if there is one.

        Pre-UI installs authorised from a terminal and have no refresh
        token in their account blob; this keeps them syncing untouched.
        """
        path = self._tokenstore_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f).get("refresh_token")
        except (OSError, ValueError) as e:
            print(f"[google_health] could not read {path}: {e}")
            return None

    def _refresh_token(self, credentials):
        """The refresh token to sync with: the stored account, else disk."""
        token = credentials.get("refresh_token") or self._legacy_refresh_token()
        if not token:
            raise RuntimeError(
                "Google Health is not authorised yet. Add it under Config -> "
                "Connected sources and approve access on Google's consent screen."
            )
        return token

    def _get_access_token(self, conn=None):
        # Same source of truth as every other plugin: the stored account
        # if there is one, the env vars otherwise. Since the UI flow, the
        # refresh token lives in that same encrypted blob.
        creds = self.get_credentials(conn)
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": creds.get("client_id"),
                "client_secret": creds.get("client_secret"),
                "refresh_token": self._refresh_token(creds),
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    # ---- oauth (see SyncPlugin.oauth_auth_url / oauth_exchange) ----

    def oauth_auth_url(self, credentials, redirect_uri, code_challenge):
        params = {
            "client_id": credentials.get("client_id"),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # offline + a forced consent screen is what makes Google hand
            # back a refresh token rather than an access token alone.
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return AUTH_URL + "?" + urllib.parse.urlencode(params)

    def oauth_exchange(self, credentials, code, code_verifier, redirect_uri):
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": credentials.get("client_id"),
                "client_secret": credentials.get("client_secret"),
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
        if not resp.ok:
            # Google says why in the body ("invalid_grant" for a stale or
            # reused code); pass that through rather than a bare 400.
            raise RuntimeError(f"Google rejected the authorization code: {resp.text}")
        tokens = resp.json()
        # Only the long-lived half is worth keeping - access tokens expire
        # in an hour and every sync mints a fresh one anyway.
        return {
            "client_id": credentials.get("client_id"),
            "client_secret": credentials.get("client_secret"),
            "refresh_token": tokens.get("refresh_token"),
        }

    def sync(self, conn, days: int) -> int:
        access_token = self._get_access_token(conn)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        today = date.today()
        start = today - timedelta(days=days - 1)

        steps_by_date = self._fetch_steps(headers, start, today)
        sleep_by_date, sleep_sessions = self._fetch_sleep(headers, start, today)
        hr_by_date = self._fetch_resting_hr(headers, start, today)
        workouts = self._fetch_workouts(headers, start)

        all_dates = set(steps_by_date) | set(sleep_by_date) | set(hr_by_date)
        written = 0
        for d_str in all_dates:
            # Every date here has at least one reading; the metrics it
            # lacks come back None and are left as they were.
            write_metric(conn, d_str, self.id, STEPS, steps_by_date.get(d_str))
            write_metric(conn, d_str, self.id, RESTING_HR_BPM, hr_by_date.get(d_str))
            write_metric(conn, d_str, self.id, SLEEP_MINUTES, sleep_by_date.get(d_str))
            written += 1

        # Each night (and nap) as its own session, beside the daily total.
        for session in sleep_sessions:
            write_session(conn, self.id, SLEEP, *session)
        # Workouts are sessions only - there is no daily workout metric.
        for session in workouts:
            write_session(conn, self.id, WORKOUT, *session)

        conn.commit()
        return written

    # ---- individual data type fetchers ----

    def _fetch_steps(self, headers, start, end):
        """Daily step totals via the dailyRollUp endpoint - clean sums, no
        manual aggregation needed.

        dailyRollUp rejects a range longer than MAX_ROLLUP_DAYS, so a
        longer backfill asks for it a window at a time and merges the
        days. The windows don't overlap, and one that fails costs only
        its own days.
        """
        out = {}
        for window_start, window_end in _windows(start, end, MAX_ROLLUP_DAYS):
            out.update(self._fetch_steps_window(headers, window_start, window_end))
        return out

    def _fetch_steps_window(self, headers, start, end):
        """One dailyRollUp request, start to end inclusive. Its page token
        goes in the body, beside the rest of the request repeated
        unchanged."""
        body = {
            "range": {
                "start": {"date": {"year": start.year, "month": start.month, "day": start.day}},
                "end": {"date": _next_day_dict(end)},
            },
            "windowSizeDays": 1,
        }
        try:
            points = _all_pages("steps", "rollupDataPoints", lambda token: requests.post(
                f"{API_BASE}/dataTypes/steps/dataPoints:dailyRollUp",
                headers=headers, json=_with_page_token(body, token), timeout=20,
            ))
            out = {}
            for point in points:
                d = point.get("civilStartTime", {}).get("date", {})
                if not d:
                    continue
                d_str = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
                count = point.get("steps", {}).get("countSum")
                if count is not None:
                    out[d_str] = int(count)
            return out
        except Exception as e:
            print(f"[google_health] steps fetch failed for {start} to {end}: {e}")
            return {}

    def _fetch_sleep(self, headers, start, end):
        """Minutes asleep via the list endpoint, summed per civil day, plus
        each data point as a (start, end, summary) sleep session.

        Returns (minutes_by_date, sessions). The two are read from the
        same points independently, so a session that can't be parsed
        never costs the daily total. Both leave out sleep that began
        before `start` (see below).
        """
        # Sleep can't be filtered on when it starts - the discovery doc
        # rules sleep out of interval.civil_start_time - so this asks by
        # civil end date, which it lists for sleep. That also brings back
        # the night that began the evening before `start` and ended inside
        # the window. Filed under the day it began, that night alone would
        # replace the day's total - dropping any nap from earlier that day,
        # which ended before `start` and so isn't in this answer. So sleep
        # whose civil start day is before `start` is skipped, for the
        # total and the session alike: that day keeps the full total an
        # earlier sync wrote while it was inside the window, and every day
        # this sync does write has all its sleep here. (A point with no
        # civil start day can't be placed; it is kept, and as before
        # gives a session but no daily total.)
        first_day = start.isoformat()
        params = {"filter": f'sleep.interval.civil_end_time >= "{first_day}"'}
        try:
            points = _all_pages("sleep", "dataPoints", lambda token: requests.get(
                f"{API_BASE}/dataTypes/sleep/dataPoints",
                headers=headers, params=_with_page_token(params, token), timeout=20,
            ))
            out = {}
            sessions = []
            for point in points:
                sleep = point.get("sleep", {})
                civil = sleep.get("interval", {}).get("civilStartTime", {}).get("date")
                d_str = (f"{civil['year']:04d}-{civil['month']:02d}-{civil['day']:02d}"
                         if civil else None)
                # ISO dates compare in date order as strings.
                if d_str is not None and d_str < first_day:
                    continue
                session = _sleep_session(sleep)
                if session:
                    sessions.append(session)
                minutes = sleep.get("summary", {}).get("minutesAsleep")
                if d_str is None or minutes is None:
                    continue
                out[d_str] = out.get(d_str, 0) + int(minutes)
            # Same idea as the resting-HR fetcher: sessions came back but
            # not one had a stage in it, so the stage fields are probably
            # not where this expects - show what a summary looks like.
            if sessions and all(len(summary) == 1 for _, _, summary in sessions):
                sample = points[0].get("sleep", {}).get("summary")
                print(f"[google_health] sleep: got sessions but no stage breakdown in "
                      f"summary.stagesSummary. Sample summary: {sample}")
            return out, sessions
        except Exception as e:
            print(f"[google_health] sleep fetch failed: {e}")
            return {}, []

    def _fetch_resting_hr(self, headers, start, end):
        """Daily resting heart rate via the list endpoint (the type has no
        rollup).

        A daily type, so it filters on `.date` like the doc's other daily
        summaries. Each point's dailyRestingHeartRate carries the day as
        `date` and the reading as `beatsPerMinute` - an int64, so Google
        sends it as a JSON string.
        """
        params = {"filter": f'daily_resting_heart_rate.date >= "{start.isoformat()}"'}
        try:
            points = _all_pages("resting HR", "dataPoints", lambda token: requests.get(
                f"{API_BASE}/dataTypes/daily-resting-heart-rate/dataPoints",
                headers=headers, params=_with_page_token(params, token), timeout=20,
            ))
            out = {}
            for point in points:
                payload = point.get("dailyRestingHeartRate", {})
                civil = payload.get("date")
                bpm = _int_or_none(payload.get("beatsPerMinute"))
                if civil and bpm is not None:
                    d_str = f"{civil['year']:04d}-{civil['month']:02d}-{civil['day']:02d}"
                    out[d_str] = bpm
            if not out and points:
                print(f"[google_health] resting HR: got data but no date and beatsPerMinute "
                      f"in dailyRestingHeartRate. Sample point: {points[0]}")
            return out
        except Exception as e:
            print(f"[google_health] resting HR fetch failed: {e}")
            return {}

    def _fetch_workouts(self, headers, start):
        """Each exercise data point that began on or after `start` as a
        (start, end, summary) workout session.

        Unlike sleep, exercise can be filtered on its civil start date -
        the discovery doc's "session civil start time" filter names it as
        the example. And a workout is filed as a session keyed by its own
        start, with no daily total to keep whole, so there is nothing like
        sleep's window trim: every point that comes back is written.
        """
        params = {"filter": f'exercise.interval.civil_start_time >= "{start.isoformat()}"'}
        try:
            points = _all_pages("exercise", "dataPoints", lambda token: requests.get(
                f"{API_BASE}/dataTypes/exercise/dataPoints",
                headers=headers, params=_with_page_token(params, token), timeout=20,
            ))
        except Exception as e:
            print(f"[google_health] exercise fetch failed: {e}")
            return []
        sessions = []
        for point in points:
            session = _workout_session(point)
            if session:
                sessions.append(session)
        # Same idea as the sleep fetcher: workouts came back but not one
        # had a number in its summary, so the metric fields are probably
        # not where this expects - show what a point looks like.
        if sessions and all(set(summary) <= {"type"} for _, _, summary in sessions):
            print(f"[google_health] exercise: got sessions but no duration, distance, "
                  f"heart rate or calories in any. Sample point: {points[0]}")
        return sessions


def _all_pages(label, items_key, request_page):
    """Every item under items_key, following nextPageToken to the last page.

    request_page(token) makes one request and returns the response, token
    None for the first page. Where the token goes is up to the caller: the
    list GETs take it as a query param, dailyRollUp in the request body.

    A failed first page raises, for the fetcher to handle as it always
    has. A failed later page is logged and what came before is kept - the
    pages run newest first, so a partial backfill beats none. Stops after
    MAX_PAGES, logged, should a token never run out.
    """
    items = []
    token = None
    for page in range(1, MAX_PAGES + 1):
        try:
            resp = request_page(token)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get(items_key) or [])
            token = data.get("nextPageToken")
        except Exception as e:
            if page == 1:
                raise
            print(f"[google_health] {label}: page {page} failed, keeping the "
                  f"{len(items)} points from earlier pages: {e}")
            return items
        if not token:
            return items
    print(f"[google_health] {label}: stopped after {MAX_PAGES} pages with more still "
          f"to come, keeping the {len(items)} points read so far")
    return items


def _with_page_token(fields, token):
    """A request's params or body, plus the page token once there is one."""
    return {**fields, "pageToken": token} if token else fields


def _sleep_session(sleep):
    """One sleep data point as a (start, end, summary) session, or None.

    Reads interval.startTime/endTime (RFC 3339) and, from summary,
    minutesAsleep plus each stage's minutes in stagesSummary - falling
    back to minutesAwake if the AWAKE stage isn't listed. Google sends
    these int64 counts as JSON strings.

    Defensive throughout: a stage that isn't there is left out of the
    summary, a point with no minutesAsleep is no session (the daily total
    skips it too), a point with no usable start is logged and skipped,
    and nothing here raises.
    """
    try:
        sleep_summary = sleep.get("summary") or {}
        asleep = _int_or_none(sleep_summary.get("minutesAsleep"))
        if asleep is None:
            return None

        interval = sleep.get("interval") or {}
        start = _rfc3339_iso(interval.get("startTime"))
        if start is None:
            print(f"[google_health] sleep point has no usable interval.startTime - "
                  f"no session written. interval: {interval}")
            return None

        summary = {"asleep_minutes": asleep}
        for stage in sleep_summary.get("stagesSummary") or []:
            key = SLEEP_STAGE_KEYS.get(stage.get("type"))
            minutes = _int_or_none(stage.get("minutes"))
            if key and minutes is not None:
                summary[key] = summary.get(key, 0) + minutes
        awake = _int_or_none(sleep_summary.get("minutesAwake"))
        if "awake_minutes" not in summary and awake is not None:
            summary["awake_minutes"] = awake

        return start, _rfc3339_iso(interval.get("endTime")), summary
    except Exception as e:
        print(f"[google_health] could not read a sleep session: {e}")
        return None


def _workout_session(point):
    """One exercise data point as a (start, end, summary) workout session,
    or None.

    Reads, under the point's `exercise`: interval.startTime/endTime (RFC
    3339); exerciseType, an enum like RUNNING, lowercased; activeDuration,
    a duration string like "1830s" that excludes pauses; and from
    metricsSummary, distanceMillimeters, averageHeartRateBeatsPerMinute
    (an int64, so a JSON string) and caloriesKcal - each converted to the
    workout summary's unit.

    Defensive throughout: a field that isn't there is left out of the
    summary, a point with no usable start is logged and skipped, and
    nothing here raises.
    """
    try:
        exercise = point.get("exercise") or {}
        interval = exercise.get("interval") or {}
        start = _rfc3339_iso(interval.get("startTime"))
        if start is None:
            print(f"[google_health] exercise point has no usable interval.startTime - "
                  f"no session written. interval: {interval}")
            return None

        summary = {}
        exercise_type = exercise.get("exerciseType")
        if exercise_type == "EXERCISE_TYPE_UNSPECIFIED":
            exercise_type = None
        if isinstance(exercise_type, str) and exercise_type:
            summary["type"] = exercise_type.lower()
        seconds = _duration_seconds(exercise.get("activeDuration"))
        if seconds is not None:
            summary["duration_minutes"] = round(seconds / 60)
        metrics = exercise.get("metricsSummary") or {}
        millimetres = _float_or_none(metrics.get("distanceMillimeters"))
        if millimetres is not None:
            summary["distance_m"] = millimetres / 1000
        bpm = _int_or_none(metrics.get("averageHeartRateBeatsPerMinute"))
        if bpm is not None:
            summary["avg_hr_bpm"] = bpm
        kcal = _float_or_none(metrics.get("caloriesKcal"))
        if kcal is not None:
            summary["calories_kcal"] = round(kcal)

        return start, _rfc3339_iso(interval.get("endTime")), summary
    except Exception as e:
        print(f"[google_health] could not read an exercise session: {e}")
        return None


def _duration_seconds(value):
    """A Google duration ("1830s", "1830.5s") in seconds, or None."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    return _float_or_none(value[:-1])


def _rfc3339_iso(value):
    """An RFC 3339 timestamp from Google as an iso_utc() string, or None.

    Fractional seconds are dropped before parsing: Google may send 0, 3,
    6 or 9 digits of them, and sessions are stored to the whole second.
    """
    if not isinstance(value, str):
        return None
    try:
        return iso_utc(datetime.fromisoformat(re.sub(r"\.\d+", "", value)))
    except ValueError:
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _next_day_dict(d):
    nxt = d + timedelta(days=1)
    return {"year": nxt.year, "month": nxt.month, "day": nxt.day}


def _windows(start, end, max_days):
    """start to end, both inclusive, as consecutive (first, last) date
    pairs of at most max_days days each, oldest first."""
    windows = []
    while start <= end:
        last = min(start + timedelta(days=max_days - 1), end)
        windows.append((start, last))
        start = last + timedelta(days=1)
    return windows
