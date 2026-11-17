"""
Oura sync plugin (Oura API v2, https://api.ouraring.com/v2/usercollection).

Auth is OAuth 2.0 only - Oura retired personal access tokens in December
2025. You register an API application at cloud.ouraring.com/oauth/applications
with http://localhost:9109/ as its redirect URI (OuraPlugin.oauth_redirect_uri:
Oura's form takes plain http only for "localhost", so not the
127.0.0.1 spelling other OAuth sources use), then add the ring in the UI:
client id and secret, approve on Oura's consent screen, paste back the
address it sends you to. Gen3 and newer rings need an active Oura
Membership for the API to return data.

Oura's refresh tokens are single-use: every refresh hands back a new one
and invalidates the old. So unlike Google, this plugin writes its
credentials back - the new refresh token, and the access token with its
expiry, so a sync reuses it instead of rotating on every run. That write
goes through replace_credentials() (plugins/base.py): committed before any
data is fetched, and only while the account row still holds what this sync
read, so a re-authorisation or a removal mid-sync is never overwritten. If
the newest refresh token is ever lost - the connection drops between Oura
rotating it and the reply arriving - the fix is Re-authenticate in the
source's Settings popup.

What is read, and where it lands. Every daily type is asked for over the
sync window - today - (days - 1) to today, in the user's timezone - and a
document whose `day` falls outside it is dropped, so one sync writes one
consistent span:

  - daily_activity (Oura's day runs 4am to 4am, as its app shows it):
      steps -> steps, active_calories -> active_kcal,
      total_calories -> total_kcal, sedentary_time -> sedentary_min,
      medium_activity_time -> intensity_moderate_min,
      high_activity_time -> intensity_vigorous_min (seconds to minutes).
    Oura's own MET bands, filed as the WHO two-bucket split (decided Nov
    2026: a close translation, not a confirmed identity at the edges). A
    day the ring wasn't worn at all is skipped whole - see _worn().
    Not read: equivalent_walking_distance (activity expressed as walking,
    not a distance covered - law 3), low_activity_time (no key), the
    MET minutes, and the score and its contributors (law 5).
  - sleep -> one sleep session per period of type long_sleep, sleep or
    late_nap (rest and deleted are skipped - see FILED_SLEEP_TYPES), with
    the stage minutes, latency, the stage timeline from sleep_phase_5_min,
    main_sleep and the UTC offset. And, from the night's long_sleep (the
    longest, should there be two), dated by its `day` - the day it ends:
      lowest_heart_rate -> resting_hr_bpm (what Oura's app calls resting
      heart rate; from 30-second samples, so it reads lower than other
      vendors' figures), average_hrv -> hrv_ms (the night's RMSSD),
      average_breath -> respiratory_rate_brpm
  - daily_spo2: spo2_percentage.average -> spo2_pct
  - daily_readiness: temperature_deviation -> skin_temperature_deviation
    (degrees from the sleeper's own baseline; never an absolute
    skin_temperature - law 3). The readiness score itself is not read.
  - vO2_max: vo2_max -> vo2_max
  - workout -> one workout session each: type (Oura's activity,
    lowercased), distance_m, calories_kcal. No duration_minutes - Oura
    gives only the start and end, which include pauses - and no
    avg_hr_bpm, which Oura doesn't report for a workout (law 2).
  - heartrate -> heart_rate samples, each at its own time, as Oura records
    them (about every 5 minutes at rest, every few seconds in a workout),
    from midnight on the window's first day up to now, MAX_SERIES_DAYS a
    request.

Not read at all: the readiness, sleep and activity scores, stress,
resilience and cardiovascular age (vendor composites, law 5); personal_info
(height and weight as typed into the profile, not measured); tags and
sessions. No SpO2 or respiration series exists in the API.

Each type is its own fetch: one that fails - a scope not granted, a 429 -
is logged and costs only its own data. A refresh that fails raises, which
fails the sync loudly. Every request carries a User-Agent: Oura refuses
one without.
"""

import math
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

import clock
from clock import local_midnight, today_in, user_zone
from metrics import (
    ACTIVE_KCAL,
    HEART_RATE,
    HRV_MS,
    INTENSITY_MODERATE_MIN,
    INTENSITY_VIGOROUS_MIN,
    PLAUSIBLE_RANGES,
    RESPIRATORY_RATE_BRPM,
    RESTING_HR_BPM,
    SEDENTARY_MIN,
    SKIN_TEMPERATURE_DEVIATION,
    SLEEP,
    SPO2_PCT,
    STEPS,
    TOTAL_KCAL,
    VO2_MAX,
    WORKOUT,
)

from ..base import SyncPlugin, iso_utc, write_metric, write_sample, write_session

AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
# Where codes and refresh tokens are traded, in the order tried. Oura's
# docs name only api.ouraring.com/oauth/token, but an application made in
# its newer developer portal (the consent redirect carries
# iss=https://moi.ouraring.com/oauth/v2/ext/oauth-anonymous) is refused
# there with invalid_client, and is served by the moi endpoint - which
# other integrations report serves the older applications too. So moi
# first: Oura burns an authorization code the first endpoint rejects, so
# the endpoint most likely to accept must go first. The legacy one stays
# as a fallback for a client moi doesn't know.
TOKEN_URLS = (
    "https://moi.ouraring.com/oauth/v2/ext/oauth-token",
    "https://api.ouraring.com/oauth/token",
)
API_BASE = "https://api.ouraring.com/v2/usercollection"
USER_AGENT = "OpenFit (self-hosted)"

# Only what sync() reads. Oura's docs name the SpO2 scope spo2Daily, but
# integrators who use it report the authorize endpoint wants "spo2"; if
# consent rejects it, this is the one line to change. No email or
# personal: nothing here reads them.
SCOPES = ["daily", "heartrate", "workout", "spo2"]

# A stored access token is reused until it is this close to expiring.
# Oura's last about 30 days (expires_in); refreshing only near the end
# keeps rotations - each a moment a lost reply could cost the token - rare.
ACCESS_TOKEN_MARGIN_SECONDS = 3600
# Assumed when a token reply carries no usable expires_in: a day, the
# shortest Oura has been seen to issue.
DEFAULT_TOKEN_SECONDS = 86400

# Oura hands back next_token while more pages remain; this only stops one
# that never runs out.
MAX_PAGES = 100

# The longest span one heartrate request asks for. Oura documents no cap,
# and a week of samples is a few thousand rows - a handful of pages.
MAX_SERIES_DAYS = 7

# The sleep period types filed as sleep sessions. long_sleep is the night;
# sleep a shorter sleep; late_nap a nap late in the day. "rest" - lying
# still, often with little or no sleep - and "deleted" are skipped
# (decided Nov 2026).
FILED_SLEEP_TYPES = ("long_sleep", "sleep", "late_nap")
MAIN_SLEEP_TYPE = "long_sleep"

# sleep_phase_5_min: one character per 5 minutes from bedtime_start.
SLEEP_PHASE_SECONDS = 300
SLEEP_PHASE_NAMES = {"1": "deep", "2": "light", "3": "rem", "4": "awake"}

# The sleep period's durations (seconds) -> the session summary's minutes.
SLEEP_DURATIONS = {
    "total_sleep_duration": "asleep_minutes",
    "light_sleep_duration": "light_minutes",
    "deep_sleep_duration": "deep_minutes",
    "rem_sleep_duration": "rem_minutes",
    "awake_time": "awake_minutes",
    "latency": "latency_minutes",
}

# The night's long_sleep fields filed as daily vitals, on its `day`.
NIGHTLY_VITALS = {
    "lowest_heart_rate": RESTING_HR_BPM,
    "average_hrv": HRV_MS,
    "average_breath": RESPIRATORY_RATE_BRPM,
}


def _minutes(seconds):
    return seconds / 60


# daily_activity field -> (metric, convert). Totals for Oura's 4am-to-4am
# day; the times are seconds.
ACTIVITY_FIELDS = {
    "steps": (STEPS, None),
    "active_calories": (ACTIVE_KCAL, None),
    "total_calories": (TOTAL_KCAL, None),
    "sedentary_time": (SEDENTARY_MIN, _minutes),
    "medium_activity_time": (INTENSITY_MODERATE_MIN, _minutes),
    "high_activity_time": (INTENSITY_VIGOROUS_MIN, _minutes),
}

# The rest of the daily types, a value per document: (collection, path to
# the value, metric).
DAILY_VALUES = [
    ("daily_spo2", ("spo2_percentage", "average"), SPO2_PCT),
    ("daily_readiness", ("temperature_deviation",), SKIN_TEMPERATURE_DEVIATION),
    ("vO2_max", ("vo2_max",), VO2_MAX),
]

# Whole numbers for counts and minutes; the vitals and temperature keep
# what Oura sent.
ROUNDED = {STEPS, ACTIVE_KCAL, TOTAL_KCAL, SEDENTARY_MIN, INTENSITY_MODERATE_MIN,
           INTENSITY_VIGOROUS_MIN, RESTING_HR_BPM}


class OuraPlugin(SyncPlugin):
    id = "oura"
    name = "Oura Ring"
    add_flow = "oauth"
    # Oura accepts plain http for "localhost" only - never 127.0.0.1.
    oauth_redirect_uri = "http://localhost:9109/"
    add_note = (
        "At cloud.ouraring.com/oauth/applications, create an API application "
        "and add http://localhost:9109/ as its redirect URI, exactly. Enable "
        "the daily, heartrate, workout and SpO2 scopes. Gen3 and newer rings "
        "need an active Oura Membership for the API to return data."
    )
    oauth_refresh_help = (
        "Oura didn't return a refresh token - revoke OpenFit's access in the "
        "Oura app (or at cloud.ouraring.com) and try again"
    )
    fields = [
        {"key": "client_id", "label": "Client ID", "type": "text", "required": True},
        {"key": "client_secret", "label": "Client secret", "type": "password", "required": True},
    ]

    # ---- oauth (see SyncPlugin.oauth_auth_url / oauth_exchange) ----

    def oauth_auth_url(self, credentials, redirect_uri, code_challenge):
        params = {
            "response_type": "code",
            "client_id": credentials.get("client_id"),
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            # Oura supports PKCE, S256 only; the verifier goes with the
            # exchange below.
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return AUTH_URL + "?" + urllib.parse.urlencode(params)

    def oauth_exchange(self, credentials, code, code_verifier, redirect_uri):
        resp, refusals = _post_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": credentials.get("client_id"),
            "client_secret": credentials.get("client_secret"),
            "code_verifier": code_verifier,
        })
        if resp is None:
            raise RuntimeError(
                f"Oura rejected the authorization code ({refusals}). A code works "
                "once - get a new authorization link before trying again"
            )
        tokens = resp.json()
        # Kept beside the refresh token: the first access token lasts
        # weeks, and spending the refresh token on the first sync would be
        # a rotation for nothing.
        return {
            "client_id": credentials.get("client_id"),
            "client_secret": credentials.get("client_secret"),
            **_token_fields(tokens, None),
        }

    # ---- tokens ----

    def _access_token(self, conn, force_refresh=False):
        """An access token: the stored one while it has time left, else a
        fresh one, the rotated credentials stored before it is returned.

        Raises if the account isn't authorised, if Oura refuses the
        refresh, or if the account row changed while this sync ran.
        """
        stored = self.stored_credentials(conn)
        if not stored or not stored.get("refresh_token"):
            raise RuntimeError(
                "Oura is not authorised yet. Add it under Data -> Connected "
                "sources and approve access on Oura's consent screen."
            )
        now = clock.now_utc().timestamp()
        token = stored.get("access_token")
        expires_at = stored.get("access_token_expires_at")
        if (not force_refresh and isinstance(token, str) and token
                and _is_number(expires_at)
                and expires_at - ACCESS_TOKEN_MARGIN_SECONDS > now):
            return token

        resp, refusals = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": stored["refresh_token"],
            "client_id": stored.get("client_id"),
            "client_secret": stored.get("client_secret"),
        })
        if resp is None:
            raise RuntimeError(
                f"Oura refused the refresh token ({refusals}) - "
                "use Re-authenticate in the Oura source's Settings"
            )
        fields = _token_fields(resp.json(), stored["refresh_token"])
        if not fields["access_token"]:
            raise RuntimeError("Oura's token reply had no access token")
        # The old refresh token is spent now: the new one must be on disk
        # before anything else can fail.
        if not self.replace_credentials(conn, stored, {**stored, **fields}):
            raise RuntimeError(
                "The Oura account changed while this sync ran (re-authorised or "
                "removed) - stopping; the next sync uses the account as it is now"
            )
        print("[oura] refreshed the access token and stored the rotated refresh token")
        return fields["access_token"]

    def sync(self, conn, days: int) -> int:
        api = _Api(self, conn)

        zone = user_zone(conn)
        today = today_in(zone)
        start = today - timedelta(days=days - 1)
        first, last = start.isoformat(), today.isoformat()
        # Oura's end_date is asked for a day past the window - whether it's
        # inclusive isn't documented - and the window is applied here, on
        # each document's `day`.
        dates = {"start_date": first, "end_date": (today + timedelta(days=1)).isoformat()}

        def fetch(collection):
            try:
                docs = api.collection(collection, dates)
            except _AuthError:
                raise
            except Exception as e:
                print(f"[oura] {collection} fetch failed: {e}")
                return []
            kept = [d for d in docs if isinstance(d, dict) and first <= str(d.get("day")) <= last]
            if len(kept) < len(docs):
                print(f"[oura] {collection}: dropped {len(docs) - len(kept)} documents dated "
                      f"outside {first} to {last}")
            return kept

        daily = {}  # date -> {metric: value}

        def put(day, metric, raw, label):
            value = _plausible(metric, raw)
            if value is None:
                if raw is not None:
                    low, high = PLAUSIBLE_RANGES[metric]
                    print(f"[oura] {label} on {day}: {raw!r} not filed as {metric} - "
                          f"not a number, or outside {low}..{high}")
                return
            daily.setdefault(day, {})[metric] = value

        for doc in fetch("daily_activity"):
            if not _worn(doc):
                print(f"[oura] {doc['day']}: ring not worn - skipping its activity totals")
                continue
            for field, (metric, convert) in ACTIVITY_FIELDS.items():
                raw = doc.get(field)
                if _is_number(raw) and convert:
                    raw = convert(raw)
                put(doc["day"], metric, raw, f"daily_activity.{field}")

        sleep_docs = fetch("sleep")
        sessions = []
        for doc in sleep_docs:
            if doc.get("type") not in FILED_SLEEP_TYPES:
                continue
            session = _sleep_session(doc)
            if session:
                sessions.append(session)
        for day, night in _main_nights(sleep_docs).items():
            for field, metric in NIGHTLY_VITALS.items():
                put(day, metric, night.get(field), f"sleep.{field}")

        for collection, path, metric in DAILY_VALUES:
            for doc in fetch(collection):
                put(doc["day"], metric, _dig(doc, path), f"{collection}.{'.'.join(path)}")

        workouts = [w for w in (_workout_session(d) for d in fetch("workout")) if w]

        lo = local_midnight(start, zone)
        hi = min(local_midnight(today + timedelta(days=1), zone),
                 clock.now_utc().replace(microsecond=0))
        samples = _heart_rate(api, lo, hi)

        for day, values in daily.items():
            for metric, value in values.items():
                write_metric(conn, day, self.id, metric, value)
        for session in sessions:
            write_session(conn, self.id, SLEEP, *session)
        for session in workouts:
            write_session(conn, self.id, WORKOUT, *session)
        for ts, value in samples:
            write_sample(conn, self.id, HEART_RATE, ts, value)
        conn.commit()

        print(f"[oura] {len(daily)} days of daily metrics, {len(sessions)} sleep sessions, "
              f"{len(workouts)} workouts, {len(samples)} heart-rate samples")
        sleep_days = {str(d.get("day")) for d in sleep_docs if d.get("type") in FILED_SLEEP_TYPES}
        return len(set(daily) | sleep_days)


class _AuthError(RuntimeError):
    """Oura refused this sync's token even after a refresh: the sync fails."""


class _Api:
    """One sync's requests: the bearer token, the User-Agent, pagination,
    and one refresh-and-retry when Oura answers 401 to a token it issued."""

    def __init__(self, plugin, conn):
        self.plugin, self.conn = plugin, conn
        self.token = plugin._access_token(conn)
        self.refreshed = False

    def get(self, path, params):
        for attempt in (1, 2):
            resp = requests.get(
                f"{API_BASE}/{path}",
                headers={"Authorization": f"Bearer {self.token}",
                         "User-Agent": USER_AGENT, "Accept": "application/json"},
                params=params, timeout=20,
            )
            if resp.status_code != 401:
                break
            if self.refreshed:
                raise _AuthError(f"Oura refused the access token for {path} (401)")
            # A stored token Oura no longer honours (revoked, or expired
            # early): refresh once for the whole sync and ask again.
            self.token = self.plugin._access_token(self.conn, force_refresh=True)
            self.refreshed = True
        resp.raise_for_status()
        return resp.json()

    def collection(self, path, params):
        """Every document under `data`, following next_token to the last
        page. A failed first page raises; a failed later page is logged and
        the pages before it kept. Stops after MAX_PAGES, logged."""
        items, token = [], None
        for page in range(1, MAX_PAGES + 1):
            try:
                body = self.get(path, {**params, "next_token": token} if token else params)
                items.extend(body.get("data") or [])
                token = body.get("next_token")
            except _AuthError:
                raise
            except Exception as e:
                if page == 1:
                    raise
                print(f"[oura] {path}: page {page} failed, keeping the {len(items)} "
                      f"documents from earlier pages: {e}")
                return items
            if not token:
                return items
        print(f"[oura] {path}: stopped after {MAX_PAGES} pages with more still to come")
        return items


# ---- tokens ----


def _post_token(data):
    """POST a token request to each of TOKEN_URLS in turn, until one
    accepts. Returns (response, None) for the first that does, else (None,
    what each said). A refusal or a failed request moves on to the next;
    nothing here raises. Credentials go in the form body, which Oura
    documents for both endpoints."""
    refusals = []
    for url in TOKEN_URLS:
        try:
            resp = requests.post(url, data=data, headers={"User-Agent": USER_AGENT},
                                 timeout=15)
        except Exception as e:
            refusals.append(f"{url}: {e}")
            continue
        if resp.ok:
            if refusals:
                print(f"[oura] token request accepted by {url} after: {'; '.join(refusals)}")
            return resp, None
        refusals.append(f"{url}: {resp.status_code} {resp.text[:200]}")
    return None, "; ".join(refusals)


# ---- readers ----


def _token_fields(tokens, previous_refresh):
    """A token reply as the credentials it updates: the access token, when
    it expires (epoch seconds) and the refresh token - the new one, else
    the one just used (a reply without one rotates nothing)."""
    seconds = tokens.get("expires_in")
    if not _is_number(seconds) or seconds <= 0:
        seconds = DEFAULT_TOKEN_SECONDS
    return {
        "refresh_token": tokens.get("refresh_token") or previous_refresh,
        "access_token": tokens.get("access_token"),
        "access_token_expires_at": clock.now_utc().timestamp() + seconds,
    }


def _worn(doc):
    """Whether the ring was worn at all on a daily_activity day.

    A document for a day in the drawer would carry steps 0, a
    resting-rate total_calories and the like - defaults, not readings
    (law 2) - so such a day files nothing. (Whether Oura sends one for
    an unworn day at all is to be confirmed on a real account; the gate
    costs nothing if it doesn't.)
    class_5_min marks every 5 minutes of the day, '0' for non-wear: worn
    if any mark is something else. Without it, non_wear_time: worn unless
    it covers the whole day. With neither, taken as worn - an unknown
    shape isn't evidence of a drawer.
    """
    marks = doc.get("class_5_min")
    if isinstance(marks, str) and marks:
        return marks.strip("0") != ""
    non_wear = doc.get("non_wear_time")
    if _is_number(non_wear):
        return non_wear < 86400
    return True


def _main_nights(docs):
    """{day: the long_sleep document} - the longest if a day has two."""
    nights = {}
    for doc in docs:
        if doc.get("type") != MAIN_SLEEP_TYPE:
            continue
        day = str(doc.get("day"))
        length = doc.get("total_sleep_duration") or 0
        if day not in nights or length > (nights[day].get("total_sleep_duration") or 0):
            nights[day] = doc
    return nights


def _sleep_session(doc):
    """One sleep period as a (start, end, summary) session, or None.

    The minutes are Oura's own totals (never recomputed from the
    timeline); a period without total_sleep_duration, or without a usable
    bedtime_start, is no session. Never raises.
    """
    try:
        begun = _moment(doc.get("bedtime_start"))
        if begun is None or not _is_number(doc.get("total_sleep_duration")):
            if begun is None:
                print(f"[oura] sleep {doc.get('id')}: no usable bedtime_start - no session")
            return None
        ended = _moment(doc.get("bedtime_end"))
        summary = {}
        for field, key in SLEEP_DURATIONS.items():
            seconds = doc.get(field)
            if _is_number(seconds):
                summary[key] = round(seconds / 60)
        summary["main_sleep"] = doc.get("type") == MAIN_SLEEP_TYPE
        offset = begun.utcoffset()
        if offset is not None and not offset.total_seconds() % 60:
            summary["utc_offset_minutes"] = int(offset.total_seconds() // 60)
        stages = _stages(doc.get("sleep_phase_5_min"), begun, ended)
        if stages:
            summary["stages"] = stages
        return iso_utc(begun), iso_utc(ended) if ended else None, summary
    except Exception as e:
        print(f"[oura] could not read a sleep period: {e}")
        return None


def _stages(phases, begun, ended):
    """sleep_phase_5_min as the stage timeline: [{start, end, stage}].

    Each character is 5 minutes from bedtime_start; a run of one stage is
    one segment (blocks merged, as the sleep summary asks), the last cut at
    bedtime_end. An unlisted character is "unknown", logged.
    """
    if not isinstance(phases, str) or not phases:
        return []
    segments, unknown = [], set()
    for i, mark in enumerate(phases):
        stage = SLEEP_PHASE_NAMES.get(mark)
        if stage is None:
            stage = "unknown"
            unknown.add(mark)
        seg_start = begun + timedelta(seconds=i * SLEEP_PHASE_SECONDS)
        seg_end = seg_start + timedelta(seconds=SLEEP_PHASE_SECONDS)
        if ended is not None:
            if seg_start >= ended:
                break
            seg_end = min(seg_end, ended)
        if segments and segments[-1]["stage"] == stage:
            segments[-1]["end"] = iso_utc(seg_end)
        else:
            segments.append({"start": iso_utc(seg_start), "end": iso_utc(seg_end), "stage": stage})
    if unknown:
        print(f"[oura] sleep at {iso_utc(begun)}: phase mark(s) {sorted(unknown)} filed as \"unknown\"")
    return segments


def _workout_session(doc):
    """One workout document as a (start, end, summary) session, or None."""
    try:
        begun = _moment(doc.get("start_datetime"))
        if begun is None:
            print(f"[oura] workout {doc.get('id')}: no usable start_datetime - no session")
            return None
        ended = _moment(doc.get("end_datetime"))
        summary = {}
        activity = doc.get("activity")
        if isinstance(activity, str) and activity.strip():
            summary["type"] = activity.strip().lower()
        if _is_number(doc.get("distance")):
            summary["distance_m"] = float(doc["distance"])
        if _is_number(doc.get("calories")):
            summary["calories_kcal"] = round(doc["calories"])
        return iso_utc(begun), iso_utc(ended) if ended else None, summary
    except Exception as e:
        print(f"[oura] could not read a workout: {e}")
        return None


def _heart_rate(api, lo, hi):
    """heartrate rows from lo to hi, MAX_SERIES_DAYS a request, as
    [(ts, bpm)]: each at its own time, a positive whole number. A window
    that fails is logged and costs only its own span."""
    samples, rejected = [], 0
    start = lo
    while start < hi:
        stop = min(start + timedelta(days=MAX_SERIES_DAYS), hi)
        try:
            rows = api.collection("heartrate", {
                "start_datetime": start.isoformat(),
                "end_datetime": stop.isoformat(),
            })
        except _AuthError:
            raise
        except Exception as e:
            print(f"[oura] heartrate fetch failed for {iso_utc(start)} to {iso_utc(stop)}: {e}")
            rows = []
        for row in rows:
            moment = _moment(row.get("timestamp")) if isinstance(row, dict) else None
            bpm = row.get("bpm") if isinstance(row, dict) else None
            if moment is None or not _is_number(bpm) or bpm <= 0 or not lo <= moment < hi:
                rejected += 1
                continue
            samples.append((iso_utc(moment), round(bpm)))
        start = stop
    if rejected:
        print(f"[oura] heartrate: {rejected} rows not filed - no usable time, no positive "
              f"bpm, or outside the window")
    return samples


def _moment(value):
    """An Oura timestamp (offset or Z, fractional seconds dropped) as an
    aware datetime keeping its offset, or None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(re.sub(r"\.\d+", "", value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _dig(doc, path):
    for key in path:
        doc = doc.get(key) if isinstance(doc, dict) else None
    return doc


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _plausible(metric, raw):
    """`raw` as a reading of `metric` - rounded where the key counts
    whole units - or None: not a finite number, or outside its range."""
    if not _is_number(raw):
        return None
    value = round(raw) if metric in ROUNDED else float(raw)
    low, high = PLAUSIBLE_RANGES[metric]
    return value if low <= value <= high else None
