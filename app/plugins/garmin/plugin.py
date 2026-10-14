"""
Garmin Connect sync plugin.

Auth: uses GARMIN_EMAIL / GARMIN_PASSWORD from environment on first login,
then caches a session token on disk (GARMIN_TOKENSTORE) so subsequent syncs
reuse it instead of logging in with the password again. If your account has
MFA enabled, run first_login.py once (see repo README) before starting the
scheduled sync.

Fetched per day: get_stats (steps, resting HR, distance, calories -
active, total and basal - floors, elevation gain, intensity and
sedentary minutes), get_sleep_data (the night as a sleep
session - there is no daily sleep metric), get_spo2_data,
get_respiration_data and get_hrv_data (the day's SpO2, respiration rate
and HRV, and from the same SpO2 and respiration responses their
intraday series, as samples), get_training_status and
get_hydration_data (VO2 max and hydration), get_body_composition
(weight, body fat, BMI, muscle and bone mass, body water and visceral
fat, from a compatible scale) and get_heart_rates (the intraday heart-rate
series, as samples). Fetched once over the whole range:
get_activities_by_date, each activity a workout session.
"""

import math
import os
import shutil
from datetime import date, datetime, timedelta, timezone

from garminconnect import Garmin, GarminConnectAuthenticationError

from metrics import (
    ACTIVE_KCAL,
    BASAL_KCAL,
    BMI,
    BODY_FAT_PCT,
    BODY_WATER_PCT,
    BONE_MASS_KG,
    DISTANCE_M,
    ELEVATION_GAIN_M,
    FLOORS,
    HEART_RATE,
    HRV_MS,
    HYDRATION_ML,
    INTENSITY_MODERATE_MIN,
    INTENSITY_VIGOROUS_MIN,
    MUSCLE_MASS_KG,
    PLAUSIBLE_RANGES,
    RESPIRATION,
    RESPIRATORY_RATE_BRPM,
    RESTING_HR_BPM,
    SEDENTARY_MIN,
    SLEEP,
    SPO2,
    SPO2_PCT,
    STEPS,
    TOTAL_KCAL,
    VISCERAL_FAT,
    VO2_MAX,
    WEIGHT_KG,
    WORKOUT,
)

from ..base import SyncPlugin, iso_utc, write_metric, write_sample, write_session

# Daily metrics from the get_stats response, beside the existing steps/
# resting HR: field -> (metric, conversion to its canonical unit). Each
# is rounded to a whole number (floorsAscended can come fractional);
# sedentarySeconds is turned into minutes first, the rest are in their
# canonical unit already. A field the response doesn't carry, or that
# isn't a number, writes nothing - so a field name that turns out wrong
# costs that one metric and nothing else, and _stats_metrics logs it.
#
# The floors, intensity, basal, sedentary and elevation field names are
# unverified guesses; floorsAscendedInMeters may not be in the daily
# summary at all. There is no active_minutes here, deliberately: Garmin
# reports the moderate/vigorous split, and a single-number "active
# minutes" is another vendor's measurement (schema v2, law 3).
STATS_METRICS = {
    "totalDistanceMeters": (DISTANCE_M, round),
    "activeKilocalories": (ACTIVE_KCAL, round),
    "totalKilocalories": (TOTAL_KCAL, round),
    "floorsAscended": (FLOORS, round),
    "moderateIntensityMinutes": (INTENSITY_MODERATE_MIN, round),
    "vigorousIntensityMinutes": (INTENSITY_VIGOROUS_MIN, round),
    "bmrKilocalories": (BASAL_KCAL, round),
    "sedentarySeconds": (SEDENTARY_MIN, lambda seconds: round(seconds / 60)),
    "floorsAscendedInMeters": (ELEVATION_GAIN_M, round),
}

# Vitals, a request a day each, and each response read for both grains:
# the day's figure as a daily metric and, where the response carries one,
# its intraday array as a series. Per entry: what to call it in a log
# line, the client method, "daily" - the path to the day's value in the
# response and the metric it files as - and "series" - the response key
# holding its [epoch_ms, value] rows and the series metric they file as,
# or None. Each value is already in its canonical unit and is stored as
# reported. A value or array that isn't there, or a value that isn't a
# number, writes nothing and isn't logged: an empty response is how a
# device without that sensor answers (get_hrv_data is {} on a watch
# without HRV), not a wrong field name. SpO2's series is the hourly
# averages - the finer spO2SingleValues can be null - and HRV's readings
# aren't a series here.
#
# It also carries two single-value daily fetches with no sensor or series
# semantics - VO2 max and hydration - since they take the same shape: one
# date argument, one value at a path, stored as reported.
GARMIN_VITALS = [
    {
        "label": "SpO2",
        "method": "get_spo2_data",
        "daily": (("averageSpO2",), SPO2_PCT),
        "series": ("spO2HourlyAverages", SPO2),
    },
    {
        "label": "respiration",
        "method": "get_respiration_data",
        "daily": (("avgWakingRespirationValue",), RESPIRATORY_RATE_BRPM),
        "series": ("respirationValuesArray", RESPIRATION),
    },
    {
        "label": "HRV",
        "method": "get_hrv_data",
        "daily": (("hrvSummary", "lastNightAvg"), HRV_MS),
        "series": None,
    },
    # get_training_status gives the most recent estimate whatever day is
    # asked, so each day of the window files the current value - fine for
    # a metric that moves this slowly.
    {
        "label": "VO2 max",
        "method": "get_training_status",
        "daily": (("mostRecentVO2Max", "generic", "vo2MaxValue"), VO2_MAX),
        "series": None,
    },
    {
        "label": "hydration",
        "method": "get_hydration_data",
        "daily": (("valueInML",), HYDRATION_ML),
        "series": None,
    },
]

# Body composition from get_body_composition's totalAverage -> the metric
# each files as. These are in their canonical units already - bodyFat and
# bodyWater percentages, bmi, and visceralFat a unitless rating - and are
# stored as reported.
BODY_COMPOSITION_FIELDS = {
    "bodyFat": BODY_FAT_PCT,
    "bmi": BMI,
    "bodyWater": BODY_WATER_PCT,
    "visceralFat": VISCERAL_FAT,
}

# The masses in totalAverage -> the metric each files as. Garmin reports
# these in grams, so each is converted to kg and range-guarded (see
# _body_composition_metrics). The muscleMass and boneMass names and units
# are unverified, like weight's.
#
# physiqueRating and metabolicAge sit beside these in totalAverage and are
# deliberately not read: they're vendor-proprietary composites, which
# schema v2 excludes (law 5).
BODY_COMPOSITION_GRAMS = {
    "weight": WEIGHT_KG,
    "muscleMass": MUSCLE_MASS_KG,
    "boneMass": BONE_MASS_KG,
}

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
            stats_metrics = {}
            sleep_minutes = None
            sleep_session = None

            try:
                stats = client.get_stats(d_str)
                steps = stats.get("totalSteps")
                resting_hr = stats.get("restingHeartRate")
                stats_metrics = _stats_metrics(stats, d_str)
            except Exception as e:
                print(f"[garmin] stats fetch failed for {d_str}: {e}")

            try:
                sleep = client.get_sleep_data(d_str)
                dto = sleep.get("dailySleepDTO") or {}
                seconds = dto.get("sleepTimeSeconds")
                if seconds:
                    sleep_minutes = round(seconds / 60)
                    # Sleep is filed as the night's session only, an
                    # interval with its stages. Never raises - a shape
                    # mismatch costs the session, not the day.
                    sleep_session = _sleep_session(dto, sleep_minutes, d_str)
            except Exception as e:
                print(f"[garmin] sleep fetch failed for {d_str}: {e}")

            # Each vital is its own request, so a failed one costs that
            # vital - its metric and its series - and nothing else. The one
            # response gives both: the daily value is held for below, and
            # the series goes straight in, ahead of the skip, as heart
            # rate's does.
            vitals = {}
            for vital in GARMIN_VITALS:
                try:
                    payload = getattr(client, vital["method"])(d_str)
                    path, metric = vital["daily"]
                    value = _daily_vital(payload, path)
                    if value is not None:
                        vitals[metric] = value
                    if vital["series"]:
                        key, series_metric = vital["series"]
                        rows = payload.get(key) if isinstance(payload, dict) else None
                        for ts, sample in _series_samples(rows):
                            write_sample(conn, self.id, series_metric, ts, sample)
                except Exception as e:
                    print(f"[garmin] {vital['label']} fetch failed for {d_str}: {e}")

            # Body composition is a range call (the day as start and end)
            # with several readings in it, so it has its own request rather
            # than a GARMIN_VITALS entry. A failed fetch costs those readings.
            body_metrics = {}
            try:
                body_metrics = _body_composition_metrics(
                    client.get_body_composition(d_str, d_str), d_str
                )
            except Exception as e:
                print(f"[garmin] body-composition fetch failed for {d_str}: {e}")

            # The intraday series goes straight in, ahead of the skip
            # below: a day with samples but no daily summary still keeps
            # its samples. A failed fetch costs the samples only.
            try:
                for ts, bpm in _heart_rate_samples(client.get_heart_rates(d_str)):
                    write_sample(conn, self.id, HEART_RATE, ts, bpm)
            except Exception as e:
                print(f"[garmin] heart-rate fetch failed for {d_str}: {e}")

            if (
                steps is None
                and resting_hr is None
                and sleep_session is None
                and not stats_metrics
                and not vitals
                and not body_metrics
            ):
                continue

            # A None (that fetch failed, or Garmin had nothing) writes
            # nothing and leaves any earlier reading in place.
            write_metric(conn, d_str, self.id, STEPS, steps)
            write_metric(conn, d_str, self.id, RESTING_HR_BPM, resting_hr)
            for metric, value in stats_metrics.items():
                write_metric(conn, d_str, self.id, metric, value)
            for metric, value in vitals.items():
                write_metric(conn, d_str, self.id, metric, value)
            for metric, value in body_metrics.items():
                write_metric(conn, d_str, self.id, metric, value)
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


def _stats_metrics(stats, d_str):
    """The STATS_METRICS readings in a get_stats response, as {metric: value}.

    Only the fields that are there as a finite number make it in, each
    converted to its canonical unit; anything else is left out, as if
    Garmin had reported nothing. A field missing from the response
    altogether (not just null, which is a day without that data) is
    logged with the keys that are there, since it most likely means a
    field name that's wrong. sync() reads steps and resting HR before
    calling this, so nothing here can cost them.
    """
    if not isinstance(stats, dict) or not stats:
        return {}
    out = {}
    for field, (metric, convert) in STATS_METRICS.items():
        value = stats.get(field)
        if _is_number(value):
            out[metric] = convert(value)
    missing = [field for field in STATS_METRICS if field not in stats]
    if missing:
        print(
            f"[garmin] stats for {d_str} have no {', '.join(missing)} - not written. "
            f"stats keys: {sorted(stats)}"
        )
    return out


def _body_composition_metrics(payload, d_str):
    """The readings in a get_body_composition response's totalAverage, as
    {metric: value}.

    BODY_COMPOSITION_FIELDS (bodyFat, bmi, bodyWater, visceralFat) are
    stored as reported. BODY_COMPOSITION_GRAMS (weight, muscleMass,
    boneMass) are converted from grams to kg (Garmin reports
    body-composition masses in grams). That unit can't be checked until a
    compatible scale reports, so each converted mass is written only if
    it's within its metric's plausible range; a finite mass that converts
    outside it writes nothing and logs the raw and converted values, so
    the unit can be confirmed from the logs. Like _daily_vital: a payload
    or totalAverage that's missing, null or not a dict, or a field that
    isn't a finite number, is left out without a log line - a day with no
    weigh-in, or an account with no compatible scale, comes back like
    that. Never raises.
    """
    try:
        average = payload.get("totalAverage") if isinstance(payload, dict) else None
        if not isinstance(average, dict):
            return {}
        out = {}
        for field, metric in BODY_COMPOSITION_FIELDS.items():
            value = average.get(field)
            if _is_number(value):
                out[metric] = value
        for field, metric in BODY_COMPOSITION_GRAMS.items():
            grams = average.get(field)
            if not _is_number(grams):
                continue
            kg = grams / 1000.0
            low, high = PLAUSIBLE_RANGES[metric]
            if low <= kg <= high:
                out[metric] = kg
            else:
                print(
                    f"[garmin] body-composition {field} {grams}g -> {kg}kg out of "
                    f"plausible range, not written ({d_str})"
                )
        return out
    except Exception as e:
        print(f"[garmin] could not read body composition for {d_str}: {e}")
        return {}


def _daily_vital(payload, path):
    """The number at `path` (a tuple of keys) in a vitals response, or None.

    Walks the nested dicts defensively: a payload that's None or {}, a
    level that's missing or null, or a value that isn't a finite number
    all read as None - a day without that vital. Never raises.
    """
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if _is_number(value) else None


def _is_number(value):
    """A finite int or float - not a bool, a string or NaN."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _sleep_session(dto, asleep_minutes, d_str):
    """The night in a dailySleepDTO as a (start, end, summary) session, or None.

    The interval comes from sleepStartTimestampGMT/sleepEndTimestampGMT
    (epoch milliseconds, real UTC - the *Local pair is shifted by the
    timezone and garminconnect warns it can be shifted twice), and each
    stage from its *SleepSeconds field, rounded to whole minutes.
    asleep_minutes is sleepTimeSeconds in whole minutes - sleep has no
    daily metric, so the session is where the night's total lives.

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


def _heart_rate_samples(payload):
    """The intraday [epoch_ms, bpm] pairs in a get_heart_rates payload as
    (iso_utc timestamp, bpm) tuples.

    heartRateValues runs on a steady ~2-minute cadence, index 0 the
    timestamp and index 1 the heart rate (heartRateValueDescriptors says
    so); a bpm of null marks non-wear, and the first pair often is one.
    The daily max/min/restingHeartRate beside it are ignored here -
    resting HR comes in through get_stats.

    Drops null bpm (a non-wear gap - write_sample would skip it anyway, but
    there's no reason to call it) and anything unparseable, a bpm that
    isn't a number included. Defensive throughout; never raises - a shape
    mismatch costs the samples, not the sync.
    """
    if not isinstance(payload, dict):
        return []
    values = payload.get("heartRateValues")
    if not isinstance(values, list):
        return []
    out = []
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        ts_ms, bpm = pair[0], pair[1]
        if not isinstance(bpm, (int, float)) or not isinstance(ts_ms, (int, float)):
            continue
        ts = _gmt_iso(ts_ms)
        if ts is not None:
            out.append((ts, bpm))
    return out


def _series_samples(rows, drop_non_positive=True):
    """[[epoch_ms, value], ...] rows -> (iso_utc ts, value) pairs.

    The series counterpart of _daily_vital: Garmin's intraday arrays
    (respirationValuesArray, spO2HourlyAverages) share this shape, index
    0 the timestamp and index 1 the value (their descriptor lists say
    so). Drops nulls, non-numbers, and (by default) non-positive values -
    Garmin's "no reading" sentinels in these arrays are negative (-1,
    -2). A row that isn't a pair, or a timestamp out of range, is
    skipped. Never raises - a shape mismatch costs the samples, not the
    sync.
    """
    out = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        ts_ms, value = row[0], row[1]
        if not _is_number(ts_ms) or not _is_number(value):
            continue
        if drop_non_positive and value <= 0:
            continue
        ts = _gmt_iso(ts_ms)
        if ts is not None:
            out.append((ts, value))
    return out


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
