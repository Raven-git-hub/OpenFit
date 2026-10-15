"""
Google Health API sync plugin. Covers data from Pixel Watch and Fitbit
devices, which now flow through Google's newer Health API (health.googleapis.com)
rather than the legacy Fitbit Web API, which Google is deprecating in
September 2026.

Auth is standard Google OAuth 2.0 (not a password): you register an OAuth
client in Google Cloud Console, then add the device in the UI - client id
and secret, then approve on Google's consent screen and paste back the
code. That consent yields a refresh token, stored encrypted in the
accounts table alongside the client details, which this plugin trades
for a short-lived access token on each sync. An unverified (Testing-mode)
client's refresh token expires after about 7 days; the source's Settings
popup re-authorises it in place (Re-authenticate), reusing the stored
client details.

authorize.py does the same thing from a terminal and stays for installs
that were set up that way; its on-disk token file is still read as a
fallback when the stored account has no refresh token of its own.

Data types used (Google Health API v4). Every daily metric is asked for
over the same window - the sync's `days`, today - (days - 1) to today -
and a point dated outside it is dropped, so a sync writes one consistent
span across all of them (see DAILY_ROLLUPS and DAILY_SUMMARIES):
  - dailyRollUp (Google's own per-day sums, over every data source with
    non-wear excluded), at most MAX_ROLLUP_DAYS per request (14 for
    total-calories):
      steps -> steps, distance -> distance_m, floors -> floors,
      altitude -> elevation_gain_m, active-energy-burned -> active_kcal,
      total-calories -> total_kcal, active-zone-minutes -> active_minutes,
      sedentary-period -> sedentary_min
  - list, one point a day, filtered on the type's `.date`:
      daily-resting-heart-rate -> resting_hr_bpm,
      daily-heart-rate-variability -> hrv_ms, daily-vo2-max -> vo2_max,
      daily-oxygen-saturation -> spo2_pct,
      daily-respiratory-rate -> respiratory_rate_brpm
  - sleep                     -> list, each data point stored as a sleep session
                                 (no daily metric). Asked for by end date; sleep
                                 that began before the sync window is dropped
                                 (see _fetch_sleep)
  - exercise                  -> list, each data point stored as a workout
                                 session (no daily metric). Asked for by civil
                                 start date; every point that comes back is kept

The intraday series (see INTRADAY_SERIES) are filed as samples, one value
per SERIES_WINDOW_SECONDS (a minute) - the one place that resolution is
set - over the sync window's days, from local midnight on its first day
up to the current minute:
  - heart-rate        -> heart_rate, via rollUp: Google keeps it at about
                         one reading a second, and a rollUp windowed to
                         SERIES_WINDOW_SECONDS averages it per minute
                         server-side, so only the per-minute values cross
                         the wire (at most MAX_SERIES_DAYS per request)
  - oxygen-saturation -> spo2, via list: already about one a minute, and
                         only for valid minutes asleep, so each point is
                         stored as it comes, at its own time
No respiration series: Google has none, only the nightly summary above.

Not fetched: basal-energy-burned has no rollup in v4 (neither dailyRollUp
nor rollUp lists it), only raw interval points, so basal_kcal would mean
summing those here - not done until that is decided.

Every fetch reads all its pages (see _all_pages): Google answers a page at
a time, newest first, and a sleep or exercise page holds at most 25
points, so a backfill longer than that spans several. A series page holds
SERIES_PAGE_SIZE points, a day of minutes.

Field names - list filters and the fields read back - follow the v4
discovery document (https://health.googleapis.com/$discovery/rest?version=v4).
A filter on a field it doesn't list for that data type gets nothing back,
so a wrong name here costs that data type quietly.
"""

import json
import math
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import requests

from metrics import (
    ACTIVE_KCAL,
    ACTIVE_MINUTES,
    DISTANCE_M,
    ELEVATION_GAIN_M,
    FLOORS,
    HEART_RATE,
    HRV_MS,
    PLAUSIBLE_RANGES,
    RESPIRATORY_RATE_BRPM,
    RESTING_HR_BPM,
    SEDENTARY_MIN,
    SLEEP,
    SPO2,
    SPO2_PCT,
    STEPS,
    TOTAL_KCAL,
    VO2_MAX,
    WORKOUT,
)

from ..base import SyncPlugin, iso_utc, write_metric, write_sample, write_session

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

# The longest range one dailyRollUp request takes (DailyRollUpDataPointsRequest.
# range in the discovery doc): 90 days, or 14 for total-calories (and
# heart-rate, active-minutes, calories-in-heart-rate-zone, none read here).
# A longer one is rejected outright.
MAX_ROLLUP_DAYS = 90
MAX_ROLLUP_DAYS_SHORT = 14

# The daily metrics Google sums per civil day itself, read via dailyRollUp.
# Per entry: the data type (its URL segment), the key each rollup point
# carries its value under, the field(s) read from that value, the metric
# it files as and how to convert to that metric's unit - plus, where it
# differs, the longest range a request may take and how to read a field
# that isn't a plain number. Names and units are the v4 discovery doc's
# (DailyRollupDataPoint and the *RollupValue schemas); int64 fields arrive
# as JSON strings. A value of several fields is their sum, of whichever
# are present: Google leaves a zero out.
DAILY_ROLLUPS = [
    {"data_type": "steps", "key": "steps", "fields": ("countSum",),
     "metric": STEPS, "convert": round},
    {"data_type": "distance", "key": "distance", "fields": ("millimetersSum",),
     "metric": DISTANCE_M, "convert": lambda mm: round(mm / 1000)},
    {"data_type": "floors", "key": "floors", "fields": ("countSum",),
     "metric": FLOORS, "convert": round},
    # Altitude gain - the sum of the day's climbs, not height above sea level.
    {"data_type": "altitude", "key": "altitude", "fields": ("gainMillimetersSum",),
     "metric": ELEVATION_GAIN_M, "convert": lambda mm: round(mm / 1000)},
    # Both energy sums are kcalSum, documented in kilocalories - no kJ.
    {"data_type": "active-energy-burned", "key": "activeEnergyBurned", "fields": ("kcalSum",),
     "metric": ACTIVE_KCAL, "convert": round},
    {"data_type": "total-calories", "key": "totalCalories", "fields": ("kcalSum",),
     "metric": TOTAL_KCAL, "convert": round, "max_days": MAX_ROLLUP_DAYS_SHORT},
    # Active Zone Minutes come back per heart-rate zone, cardio and peak
    # minutes already counting double; their sum is the day's AZM total,
    # filed as active_minutes. Never split into moderate/vigorous minutes.
    {"data_type": "active-zone-minutes", "key": "activeZoneMinutes",
     "fields": ("sumInFatBurnHeartZone", "sumInCardioHeartZone", "sumInPeakHeartZone"),
     "metric": ACTIVE_MINUTES, "convert": round},
    # durationSum is a duration string in seconds, like "30600s".
    {"data_type": "sedentary-period", "key": "sedentaryPeriod", "fields": ("durationSum",),
     "metric": SEDENTARY_MIN, "convert": lambda seconds: round(seconds / 60),
     "parse": lambda value: _duration_seconds(value)},
]

# The daily summary types - one point a day, each dated - read via list,
# filtered on the type's `.date` (its data type in snake case). Same
# entries as DAILY_ROLLUPS, the value under the point's key beside its
# `date`. Each is already in its metric's unit; a convert of None stores
# it as reported.
DAILY_SUMMARIES = [
    # No rollup for this one; beatsPerMinute is an int64.
    {"data_type": "daily-resting-heart-rate", "key": "dailyRestingHeartRate",
     "fields": ("beatsPerMinute",), "metric": RESTING_HR_BPM, "convert": round},
    # The night's RMSSD average, in ms (deepSleepRootMeanSquare... beside it
    # is deep sleep only).
    {"data_type": "daily-heart-rate-variability", "key": "dailyHeartRateVariability",
     "fields": ("averageHeartRateVariabilityMilliseconds",), "metric": HRV_MS,
     "convert": None},
    # The daily cardio fitness score, ml/kg/min. Not vo2-max, which is
    # timestamped samples, nor run-vo2-max, from runs alone.
    {"data_type": "daily-vo2-max", "key": "dailyVo2Max", "fields": ("vo2Max",),
     "metric": VO2_MAX, "convert": None},
    # Both averaged over the night's sleep.
    {"data_type": "daily-oxygen-saturation", "key": "dailyOxygenSaturation",
     "fields": ("averagePercentage",), "metric": SPO2_PCT, "convert": None},
    {"data_type": "daily-respiratory-rate", "key": "dailyRespiratoryRate",
     "fields": ("breathsPerMinute",), "metric": RESPIRATORY_RATE_BRPM, "convert": None},
]

# The resolution intraday series are stored at: one value per this many
# seconds. Heart rate's rollUp buckets are this wide; change it here alone.
SERIES_WINDOW_SECONDS = 60

# The longest span one series request asks for: rollUp takes at most 14
# days of heart-rate (RollUpDataPointsRequest.range), and oxygen-saturation
# is asked for in the same windows. At SERIES_PAGE_SIZE - Google's default,
# a day of minutes - a full window is 14 pages, so MAX_PAGES only ever
# stops a runaway.
MAX_SERIES_DAYS = 14
SERIES_PAGE_SIZE = 1440

# The intraday series, filed as samples, each its own fetch. Per entry: the
# data type, the key each point carries its value under, the field read
# from it, the series metric it files as, and the endpoint asked: rollUp,
# a point per SERIES_WINDOW_SECONDS bucket timed by its startTime, or list,
# a point per reading timed by its sampleTime.physicalTime. Names are the
# v4 discovery doc's (RollupDataPoint/HeartRateRollupValue, DataPoint/
# OxygenSaturation). Each value is filed as a whole number.
INTRADAY_SERIES = [
    {"data_type": "heart-rate", "key": "heartRate", "field": "beatsPerMinuteAvg",
     "metric": HEART_RATE, "endpoint": "rollUp"},
    {"data_type": "oxygen-saturation", "key": "oxygenSaturation", "field": "percentage",
     "metric": SPO2, "endpoint": "list"},
]

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
    # Without prompt=consent Google issues a refresh token on first
    # consent only, and a repeat authorization comes back without one
    # until you revoke access. oauth_auth_url() always sends it, so this
    # is the fallback for the rare exchange that still returns none.
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
            # back a refresh token rather than an access token alone - on
            # every authorization, a repeat of an app already approved
            # included. An unverified (Testing-mode) client's refresh
            # token expires after about 7 days, so re-authorising is
            # routine; this is what keeps it to approve-and-paste, with
            # no revoke at myaccount.google.com first.
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

        # Every daily metric over the one window, start to today, as
        # {metric: {date: value}}. Each type is its own fetch: one that
        # fails or comes back empty costs only its own metric.
        daily = {}
        for spec in DAILY_ROLLUPS:
            daily[spec["metric"]] = self._fetch_rollup(headers, spec, start, today)
        for spec in DAILY_SUMMARIES:
            daily[spec["metric"]] = self._fetch_daily_summary(headers, spec, start, today)
        sleep_by_date, sleep_sessions = self._fetch_sleep(headers, start, today)
        workouts = self._fetch_workouts(headers, start)
        # Each series over the same days, as {metric: [(ts, value)]}; one
        # that fails or comes back empty costs only its own samples.
        series = {
            spec["metric"]: self._fetch_series(headers, spec, start, today)
            for spec in INTRADAY_SERIES
        }

        # A day with sleep counts as written: its sleep is filed below, as
        # sessions - there is no daily sleep metric. So does a day holding
        # only one daily metric.
        all_dates = set(sleep_by_date).union(*daily.values())
        written = 0
        for d_str in all_dates:
            # Every date here has at least one reading; the metrics it
            # lacks come back None and are left as they were.
            for metric, by_date in daily.items():
                write_metric(conn, d_str, self.id, metric, by_date.get(d_str))
            written += 1

        # Each night (and nap) as its own session.
        for session in sleep_sessions:
            write_session(conn, self.id, SLEEP, *session)
        # Workouts are sessions only - there is no daily workout metric.
        for session in workouts:
            write_session(conn, self.id, WORKOUT, *session)
        # Samples go in whatever the day's daily metrics hold, as Garmin's
        # do, and don't count toward the days written.
        for metric, samples in series.items():
            for ts, value in samples:
                write_sample(conn, self.id, metric, ts, value)

        conn.commit()
        return written

    # ---- individual data type fetchers ----

    def _fetch_rollup(self, headers, spec, start, end):
        """One DAILY_ROLLUPS type's daily values, start to end inclusive,
        as {date: value} - clean sums from Google, no manual aggregation.

        dailyRollUp rejects a range longer than the type's limit (90 days,
        or 14), so a longer backfill asks for it a window at a time and
        merges the days. The windows don't overlap, and one that fails is
        logged and costs only its own days.
        """
        label = spec["data_type"]
        points = []
        fetched = False
        max_days = spec.get("max_days", MAX_ROLLUP_DAYS)
        for window_start, window_end in _windows(start, end, max_days):
            try:
                points.extend(self._rollup_window(headers, label, window_start, window_end))
                fetched = True
            except Exception as e:
                print(f"[google_health] {label} fetch failed for {window_start} to "
                      f"{window_end}: {e}")
        if not fetched:
            return {}
        return _daily_values(spec, points, start, end, _rollup_day)

    def _rollup_window(self, headers, data_type, start, end):
        """One dailyRollUp request's points, start to end inclusive. Its
        page token goes in the body, beside the rest of the request
        repeated unchanged. Raises if the first page fails."""
        body = {
            "range": {
                "start": {"date": {"year": start.year, "month": start.month, "day": start.day}},
                "end": {"date": _next_day_dict(end)},
            },
            "windowSizeDays": 1,
        }
        return _all_pages(data_type, "rollupDataPoints", lambda token: requests.post(
            f"{API_BASE}/dataTypes/{data_type}/dataPoints:dailyRollUp",
            headers=headers, json=_with_page_token(body, token), timeout=20,
        ))

    def _fetch_daily_summary(self, headers, spec, start, end):
        """One DAILY_SUMMARIES type's daily values via the list endpoint,
        start to end inclusive, as {date: value}.

        A daily type, so it filters on `.date`, which takes >= and < (not
        <=) joined by AND - so the window's end is asked for as < the day
        after. A failed fetch is logged and costs only this type.
        """
        label = spec["data_type"]
        field = label.replace("-", "_")
        after_end = (end + timedelta(days=1)).isoformat()
        params = {"filter": f'{field}.date >= "{start.isoformat()}" AND '
                            f'{field}.date < "{after_end}"'}
        try:
            points = _all_pages(label, "dataPoints", lambda token: requests.get(
                f"{API_BASE}/dataTypes/{label}/dataPoints",
                headers=headers, params=_with_page_token(params, token), timeout=20,
            ))
        except Exception as e:
            print(f"[google_health] {label} fetch failed: {e}")
            return {}
        return _daily_values(spec, points, start, end, lambda point: _summary_day(spec, point))

    def _fetch_sleep(self, headers, start, end):
        """Each data point via the list endpoint as a (start, end, summary)
        sleep session, plus minutes asleep summed per civil day.

        Returns (minutes_by_date, sessions). Sleep is filed as sessions
        only - there is no daily sleep metric - so sync() uses
        minutes_by_date just to count the days that had sleep. The two
        are read from the same points independently, and both leave out
        sleep that began before `start` (see below).
        """
        # Sleep can't be filtered on when it starts - the discovery doc
        # rules sleep out of interval.civil_start_time - so this asks by
        # civil end date, which it lists for sleep. That also brings back
        # the night that began the evening before `start` and ended inside
        # the window, but not any nap from earlier that day, which ended
        # before `start`. So sleep whose civil start day is before `start`
        # is skipped: that day was synced whole while it was inside the
        # window, and every day this sync covers has all its sleep here.
        # (A point with no civil start day can't be placed; it is kept, and
        # gives a session but no day.)
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

    def _fetch_series(self, headers, spec, start, end):
        """One INTRADAY_SERIES type's samples, from local midnight on
        `start` to local midnight after `end` or the start of the current
        minute, whichever is sooner, as [(ts, value)]: ts an iso_utc()
        timestamp, value a whole number.

        The days are the sync window's, in the server's local time - the
        zone date.today() reads. The rest of today holds nothing yet, so
        it isn't asked for - a rollUp could answer an empty bucket for
        every minute of it - and a minute still under way is left to the
        next sync. Asked for MAX_SERIES_DAYS at a time; each
        window's points are read as they arrive, so a long backfill never
        holds more than one window's raw points, and a window that fails
        is logged and costs only its own span.

        Logged like the daily types, one line unless something was off:
        how many samples over what span, with the latest raw value beside
        the one filed; or no data; or - points but none with the field -
        a sample point and its keys. Points not filed (no usable time, not
        a finite number, or not positive - no reading) and points timed
        outside the window are logged too.
        """
        label, metric = spec["data_type"], spec["metric"]
        lo = _local_midnight(start)
        hi = min(_local_midnight(end + timedelta(days=1)),
                 _now().replace(second=0, microsecond=0))
        first, last = iso_utc(lo), iso_utc(hi)
        samples = []
        earliest = latest = example = first_rejected = None
        seen = rejected = outside = 0
        fetched = False
        for window_start, window_end in _spans(lo, hi, timedelta(days=MAX_SERIES_DAYS)):
            try:
                points = self._series_window(headers, spec, window_start, window_end)
                fetched = True
            except Exception as e:
                print(f"[google_health] {label} fetch failed for {iso_utc(window_start)} "
                      f"to {iso_utc(window_end)}: {e}")
                continue
            seen += len(points)
            if example is None and points:
                example = points[0]
            for point in points:
                ts, value, raw = _series_reading(spec, point)
                if raw is None:
                    continue
                # ISO UTC timestamps compare in time order as strings.
                if ts is not None and not first <= ts < last:
                    outside += 1
                    continue
                if ts is None or value is None:
                    rejected += 1
                    first_rejected = first_rejected or (ts, raw)
                    continue
                samples.append((ts, value))
                earliest = min(earliest or ts, ts)
                if latest is None or ts > latest[0]:
                    latest = (ts, raw, value)
        if not fetched:
            return []

        if samples:
            ts, raw, value = latest
            print(f"[google_health] {label} -> {metric}: {len(samples)} samples from "
                  f"{earliest} to {ts}, latest: {{{spec['field']!r}: {raw!r}}} -> {value}")
        elif not seen:
            print(f"[google_health] {label}: no data from {first} to {last}")
        elif not rejected and not outside:
            keys = sorted(example) if isinstance(example, dict) else type(example).__name__
            print(f"[google_health] {label}: got data but no {spec['field']} in {spec['key']}. "
                  f"Sample point: {example} (keys: {keys})")
        if rejected:
            print(f"[google_health] {label}: {rejected} not filed as {metric} - no usable time, "
                  f"not a finite number, or not positive. First: {first_rejected}")
        if outside:
            print(f"[google_health] {label}: dropped {outside} points timed outside "
                  f"{first} to {last}")
        return samples

    def _series_window(self, headers, spec, start, end):
        """One series request's points, start (inclusive) to end
        (exclusive), following every page. rollUp takes its range, bucket
        width and page token in the body; list takes a filter on the
        sample's physical time, and its token, as query params. Raises if
        the first page fails."""
        label = spec["data_type"]
        if spec["endpoint"] == "rollUp":
            body = {
                "range": {"startTime": iso_utc(start), "endTime": iso_utc(end)},
                "windowSize": f"{SERIES_WINDOW_SECONDS}s",
                "pageSize": SERIES_PAGE_SIZE,
            }
            return _all_pages(label, "rollupDataPoints", lambda token: requests.post(
                f"{API_BASE}/dataTypes/{label}/dataPoints:rollUp",
                headers=headers, json=_with_page_token(body, token), timeout=20,
            ))
        field = f"{label.replace('-', '_')}.sample_time.physical_time"
        params = {
            "filter": f'{field} >= "{iso_utc(start)}" AND {field} < "{iso_utc(end)}"',
            "pageSize": SERIES_PAGE_SIZE,
        }
        return _all_pages(label, "dataPoints", lambda token: requests.get(
            f"{API_BASE}/dataTypes/{label}/dataPoints",
            headers=headers, params=_with_page_token(params, token), timeout=20,
        ))


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


def _daily_values(spec, points, start, end, day_of):
    """A DAILY_ROLLUPS or DAILY_SUMMARIES type's points as {date: value},
    in its metric's unit.

    A point counts if the dict under the spec's key holds any of its
    fields; day_of(point) gives its civil date. The value is _reading()'s.
    A point dated outside start..end is dropped, so every type files the
    same window whatever comes back. A point with a value but no usable
    date, a value that isn't a finite number, or one outside the metric's
    plausible range writes nothing. Nothing here raises.

    What came back is logged, one line a type unless something was off,
    so a sync's logs say which types returned data: the days filed with
    the latest raw fields beside the converted value; or no data at all;
    or - points but none with the fields - a sample point and its keys,
    the mark of a wrong field name. Values not filed, and points dropped
    for their date, are logged too.
    """
    label, key, metric = spec["data_type"], spec["key"], spec["metric"]
    first, last = start.isoformat(), end.isoformat()
    out, raw = {}, {}
    rejected, outside = [], 0
    for point in points:
        try:
            payload = point.get(key) if isinstance(point, dict) else None
            if not isinstance(payload, dict):
                continue
            present = {f: payload[f] for f in spec["fields"] if payload.get(f) is not None}
            if not present:
                continue
            d_str = _civil_date(day_of(point))
            if d_str is not None and not first <= d_str <= last:
                outside += 1
                continue
            value = _reading(spec, present)
            if d_str is None or value is None:
                rejected.append((d_str, present))
                continue
            out[d_str] = value
            raw[d_str] = present
        except Exception:
            rejected.append((None, point))

    if out:
        latest = max(out)
        print(f"[google_health] {label} -> {metric}: {len(out)} days from {first} to {last}, "
              f"latest {latest}: {raw[latest]} -> {out[latest]}")
    elif not points:
        print(f"[google_health] {label}: no data from {first} to {last}")
    elif not rejected and not outside:
        sample = points[0]
        keys = sorted(sample) if isinstance(sample, dict) else type(sample).__name__
        print(f"[google_health] {label}: got data but no {'/'.join(spec['fields'])} in "
              f"{key}. Sample point: {sample} (keys: {keys})")
    if rejected:
        low, high = PLAUSIBLE_RANGES[metric]
        print(f"[google_health] {label}: {len(rejected)} not filed as {metric} - no date, "
              f"not a number, or outside {low}..{high} once converted. First: {rejected[0]}")
    if outside:
        print(f"[google_health] {label}: dropped {outside} points dated outside "
              f"{first} to {last}")
    return out


def _reading(spec, present):
    """The value of a point's fields, {field: raw value}: their sum,
    converted to the metric's unit, or None if any isn't a finite number
    or the result is outside the metric's PLAUSIBLE_RANGES."""
    parse = spec.get("parse")
    numbers = [_finite(parse(v) if parse else v) for v in present.values()]
    if None in numbers:
        return None
    total = sum(numbers)
    value = spec["convert"](total) if spec["convert"] else total
    low, high = PLAUSIBLE_RANGES[spec["metric"]]
    return value if low <= value <= high else None


def _rollup_day(point):
    """A dailyRollUp point's day: the civil date its window starts on."""
    return point.get("civilStartTime", {}).get("date")


def _summary_day(spec, point):
    """A daily summary point's day: the `date` beside its value."""
    return point[spec["key"]].get("date")


def _civil_date(civil):
    """A Google civil date ({year, month, day}) as YYYY-MM-DD, or None."""
    try:
        return date(civil["year"], civil["month"], civil["day"]).isoformat()
    except (TypeError, KeyError, ValueError):
        return None


def _finite(value):
    """A number Google sent - a JSON number, or an int64 as a JSON string -
    as a float, or None if it isn't a finite one. A bool is no number."""
    if isinstance(value, bool):
        return None
    number = _float_or_none(value)
    return number if number is not None and math.isfinite(number) else None


def _series_reading(spec, point):
    """One INTRADAY_SERIES point as (ts, value, raw): its time as an
    iso_utc() timestamp, its value rounded to a whole number, and the raw
    value read. raw is None if the point holds nothing under the spec's
    key and field - no reading. Otherwise ts is None if the point has no
    usable time, and value None if raw isn't a finite number or isn't
    positive: Google's "no reading" is no sample. Nothing here raises.
    """
    payload = point.get(spec["key"]) if isinstance(point, dict) else None
    raw = payload.get(spec["field"]) if isinstance(payload, dict) else None
    if raw is None:
        return None, None, None
    if spec["endpoint"] == "rollUp":
        moment = point.get("startTime")
    else:
        sample_time = payload.get("sampleTime")
        moment = sample_time.get("physicalTime") if isinstance(sample_time, dict) else None
    number = _finite(raw)
    value = round(number) if number is not None else None
    return _rfc3339_iso(moment), value if value is not None and value > 0 else None, raw


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


def _spans(start, end, step):
    """start to end, datetimes with end exclusive, as consecutive (first,
    stop) pairs no longer than step each, oldest first. Measured in
    elapsed time, not local days, so a span over a clock change is never
    longer than step."""
    spans = []
    while start < end:
        stop = min(start + step, end)
        spans.append((start, stop))
        start = stop
    return spans


def _local_midnight(d):
    """The moment day d begins in the server's local time - the zone
    date.today() reads - as an aware UTC datetime."""
    return datetime(d.year, d.month, d.day).astimezone(timezone.utc)


def _now():
    """The current moment, as an aware UTC datetime."""
    return datetime.now(timezone.utc)
