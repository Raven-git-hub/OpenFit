"""The schedule (SU1): a weekly run at a day, time and timezone the admin
picks, that checks - or checks, then installs what's waiting: the apps, then
OpenFit, then the Admin itself, each step the existing operation with its
own op and its own rollback.

1. The schedule's validation: the route, and its file on disk.
2. When it runs next: across days and weeks, in its own timezone, through
   the clocks going forward and back.
3. When a run is due: at the due minute, never before, never twice.
4. Missed runs: within the hour as normal; later, the check alone.
5. A check-only run: a check and nothing else.
6. An update run: its steps, in order, each a normal op.
7. Stopping at a step that doesn't end ok.
8. The lock: held from the check to the last step.
9. The Admin's step: the run finished before the hand-over.
10. Undo after a scheduled update.
11. The old weekly timer, gone.
12. The page: the Schedule panel, the last run, the notice.
13. Its routes' session and header.

As in the other Admin tests, git runs for real against temp repos (and
GitHub's stand-in), docker is conftest's FakeRunner, and the clock is fake:
it starts on Tuesday 27 October 2026, 12:00 UTC.
"""

import json
import os
import subprocess
import threading
from datetime import datetime, timezone

import pytest
from conftest import container_id
from test_admin_portal import needs_node, run_page

import admin_ops
import admin_swap

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
DEFAULT = {"day": "sun", "time": "03:00", "timezone": "UTC", "mode": "check"}
HANDOVER = "Handing over to the helper. The Admin restarts in a moment, and this page reconnects by itself."
OWNER, REPO = "Raven-git-hub", "openfit-aviation"
AVI_URL, AVI_SLUG = f"https://github.com/{OWNER}/{REPO}", "raven-git-hub__openfit-aviation"
AVIATION = {"repo.yaml": "source: openfitavi\nname: OpenFit Aviation\n", "frms/app.yaml": "id: frms\nname: FRMS\n"}
EXTRA = {"repo.yaml": "source: extra\nname: Extra\n", "walk/app.yaml": "id: walk\nname: Walk\n"}
# Whatever a check runs: git, reading only (an organisation repo's clone fetched).
READ_ONLY_GIT = {"fetch", "log", "rev-list", "diff", "rev-parse", "cat-file"}


def ts(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def at(env, stamp):
    env.clock.now = ts(stamp)


def state(env):
    return json.loads((env.data / "schedule_state.json").read_text())


def history(env, kind=None):
    return [o for o in env.admin.history() if kind is None or o["kind"] == kind]


def runs(env):
    return history(env, "scheduled_run")


def steps_of(env, run):
    """The ops a run's steps were, in the order it began them. (The history
    keeps ops in the order they were last saved, and the run is saved after
    each of its steps.)"""
    by_id = {o["id"]: o for o in env.admin.history()}
    return [by_id[s["op_id"]] for s in run["steps"] if s["op_id"]]


def texts(op):
    return [line["text"] for line in op["log"]]


def scheduled(env, **fields):
    """The schedule saved (the default but `fields`), then the baseline tick:
    the latest due time before now noted as handled, nothing run. The calls
    cleared."""
    env.admin.save_schedule({**DEFAULT, **fields})
    assert env.admin.tick() is None
    env.fake.clear()


def tick_at(env, stamp):
    at(env, stamp)
    return env.admin.tick()


def openfit_pending(env, message="New OpenFit"):
    env.repos.push({"app/main.py": f"print({message!r})\n"}, message)


def apps_pending(env, message="New app"):
    env.repos.push({"library/caffeine/app.yaml": f"id: caffeine  # {message}\n"}, message)


def admin_pending(env):
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")


def connected(env, owner=OWNER, repo=REPO, files=AVIATION):
    """An organisation repo on GitHub, added, connected and published."""
    env.github.make(owner, repo, files)
    env.admin.add_repo(f"https://github.com/{owner}/{repo}")
    env.admin.start_repo_connect(admin_ops.repo_slug(owner, repo))
    env.admin.wait(10)
    assert env.admin.current_op()["outcome"] == "ok"


def published(env, source):
    entry = admin_ops.shelf_source(admin_ops.read_shelf(str(env.library)), source)
    return entry["commit"] if entry else None


def manual_update(env):
    env.admin.check()
    env.admin.start_update()
    env.admin.wait(10)
    assert env.admin.current_op()["outcome"] == "ok"


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- 1. validation ------------------------------------------------------------------------


@pytest.mark.parametrize("field, value", [
    ("day", "sunday"), ("day", "Sun"), ("day", 6), ("day", None), ("day", ""),
    ("time", "3:00"), ("time", "24:00"), ("time", "03:60"), ("time", "03:00:00"), ("time", " 03:00"),
    ("time", 300), ("time", None), ("time", "٠٣:٠٠"), ("time", "03:00\n"),
    ("timezone", "Mars/Olympus_Mons"), ("timezone", "asia/hong_kong"), ("timezone", "localtime"),
    ("timezone", "../../etc/passwd"), ("timezone", "/usr/share/zoneinfo/UTC"), ("timezone", "UTC "),
    ("timezone", 8), ("timezone", None),
    ("mode", "auto"), ("mode", "Check"), ("mode", True), ("mode", None),
])
def test_each_bad_field_is_a_400_naming_it(logged_in, admin_env, field, value):
    r = logged_in.put("/api/schedule", json={**DEFAULT, "day": "sat", field: value}, headers=H)
    assert r.status_code == 400
    assert r.get_json() == {"ok": False, "error": admin_ops.SCHEDULE_PROBLEMS[field], "field": field}
    assert not (admin_env.data / "schedule.json").exists()
    assert not (admin_env.data / "schedule_state.json").exists()


@pytest.mark.parametrize("body, field, error", [
    ({k: v for k, v in DEFAULT.items() if k != "mode"}, "mode", admin_ops.SCHEDULE_PROBLEMS["mode"]),
    ({k: v for k, v in DEFAULT.items() if k != "day"}, "day", admin_ops.SCHEDULE_PROBLEMS["day"]),
    ({**DEFAULT, "when": "now"}, None, "'when' isn't part of a schedule."),
    ([DEFAULT], None, "Send the schedule as {day, time, timezone, mode}."),
    ("sun 03:00", None, "Send the schedule as {day, time, timezone, mode}."),
])
def test_a_body_that_isnt_a_whole_schedule_is_a_400(logged_in, admin_env, body, field, error):
    r = logged_in.put("/api/schedule", json=body, headers=H)
    assert r.status_code == 400
    assert r.get_json() == {"ok": False, "error": error, "field": field}
    assert not (admin_env.data / "schedule.json").exists()


def test_a_good_schedule_is_saved_whole(logged_in, admin_env):
    good = {"day": "sat", "time": "23:59", "timezone": "Asia/Hong_Kong", "mode": "update"}
    r = logged_in.put("/api/schedule", json=good, headers=H)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert json.loads((admin_env.data / "schedule.json").read_text()) == good
    # Saturday 31 October, 23:59 in Hong Kong (UTC+8).
    assert body["next_run"] == "2026-10-31T15:59:00Z" == body["schedule"]["next_run"]
    assert body["next_run_local"] == "Sat 31 Oct, 23:59"
    assert logged_in.get("/api/status").get_json()["schedule"] == {
        **good, "next_run": "2026-10-31T15:59:00Z", "next_run_local": "Sat 31 Oct, 23:59", "last_run": None}
    # No temp file left beside it.
    assert sorted(p.name for p in admin_env.data.iterdir() if p.name.startswith(".schedule")) == []


def test_no_file_is_the_default(admin_env):
    assert not (admin_env.data / "schedule.json").exists()
    assert admin_env.admin.schedule() == DEFAULT
    view = admin_env.admin.status()["schedule"]
    assert view == {**DEFAULT, "next_run": "2026-11-01T03:00:00Z", "next_run_local": "Sun 1 Nov, 03:00",
                    "last_run": None}


@pytest.mark.parametrize("text, named", [
    ('{"day": "sun", "time": "03:00", "timezone": "UTC"}', "mode is missing"),
    ('{"day": "funday", "time": "03:00", "timezone": "UTC", "mode": "check"}', "day isn't valid: 'funday'"),
    ('{"day": "sun", "time": "3am", "timezone": "UTC", "mode": "check"}', "time isn't valid: '3am'"),
    ('{"day": "sun", "time": "03:00", "timezone": "Nowhere/Land", "mode": "check"}', "timezone isn't valid"),
    ('{"day": "sun", "time": "03:00", "timezone": "UTC", "mode": "always"}', "mode isn't valid: 'always'"),
    ('{"day": "sun", "time": "03:00", "timezone": "UTC", "mode": "check", "when": 1}', "'when' isn't a key"),
    ("not json", "isn't a JSON object"),
    ("[]", "isn't a JSON object"),
])
def test_a_bad_schedule_file_stops_the_admin_starting(admin_env, text, named):
    (admin_env.data / "schedule.json").write_text(text)
    with pytest.raises(ValueError) as e:
        admin_env.make()
    assert "schedule.json" in str(e.value) and named in str(e.value)


RUN = {"id": "20261101T030000Z-scheduled_run", "due": "2026-11-01T03:00:00Z", "started_at": "2026-11-01T03:00:00Z",
       "finished_at": "2026-11-01T03:01:00Z", "mode": "update", "outcome": "ok", "summary": "Up to date.",
       "steps": [{"kind": "check", "op_id": None, "outcome": "ok"}], "dismissed": False}


@pytest.mark.parametrize("raw, named", [
    ({"last_due": "yesterday", "last_run": None}, "last_due isn't valid"),
    ({"last_due": None}, "isn't exactly {last_due, last_run}"),
    ({"last_due": None, "last_run": None, "more": 1}, "isn't exactly {last_due, last_run}"),
    ({"last_due": None, "last_run": {**RUN, "outcome": "great"}}, "last_run.outcome isn't valid"),
    ({"last_due": None, "last_run": {**RUN, "id": "rm -rf /"}}, "last_run.id isn't valid"),
    ({"last_due": None, "last_run": {**RUN, "dismissed": "no"}}, "last_run.dismissed isn't valid"),
    ({"last_due": None, "last_run": {**RUN, "steps": [{"kind": "rm", "op_id": None, "outcome": "ok"}]}},
     "last_run.steps[0] isn't valid"),
    ({"last_due": None, "last_run": {k: v for k, v in RUN.items() if k != "due"}}, "last_run isn't exactly"),
])
def test_a_bad_state_file_stops_the_admin_starting(admin_env, raw, named):
    (admin_env.data / "schedule_state.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError) as e:
        admin_env.make()
    assert "schedule_state.json" in str(e.value) and named in str(e.value)


def test_a_good_state_file_reads(admin_env):
    (admin_env.data / "schedule_state.json").write_text(json.dumps({"last_due": "2026-11-01T03:00:00Z",
                                                                    "last_run": RUN}))
    assert admin_env.make().schedule_state()["last_run"] == RUN


def test_the_page_lists_the_timezones_the_server_takes(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    listed = json.loads(html.split("const TIMEZONES = ", 1)[1].split(";\n", 1)[0])
    assert listed == list(admin_ops.timezones()) == sorted(listed)
    assert {"UTC", "Asia/Hong_Kong", "Europe/London", "America/New_York"} <= set(listed)
    assert "localtime" not in listed and "Factory" not in listed
    for zone in listed:
        assert admin_ops.schedule_problem("timezone", zone) is None, zone


# --- 2. the next run ------------------------------------------------------------------------


@pytest.mark.parametrize("day, time, zone, now, expected", [
    # From Tuesday 27 October, 12:00 UTC: later this week, and the week after.
    ("sun", "03:00", "UTC", "2026-10-27T12:00:00Z", "2026-11-01T03:00:00Z"),
    ("wed", "00:00", "UTC", "2026-10-27T12:00:00Z", "2026-10-28T00:00:00Z"),
    ("tue", "12:00", "UTC", "2026-10-27T12:00:00Z", "2026-10-27T12:00:00Z"),  # at now: now
    ("tue", "12:01", "UTC", "2026-10-27T12:00:00Z", "2026-10-27T12:01:00Z"),
    ("tue", "11:59", "UTC", "2026-10-27T12:00:00Z", "2026-11-03T11:59:00Z"),  # gone: next week's
    ("mon", "09:00", "UTC", "2026-10-27T12:00:00Z", "2026-11-02T09:00:00Z"),
    ("sat", "23:59", "UTC", "2026-12-27T00:00:00Z", "2027-01-02T23:59:00Z"),  # into a new year
    # In its own timezone: Sunday 03:00 there.
    ("sun", "03:00", "Asia/Hong_Kong", "2026-10-27T12:00:00Z", "2026-10-31T19:00:00Z"),
    ("sun", "03:00", "Pacific/Auckland", "2026-10-27T12:00:00Z", "2026-10-31T14:00:00Z"),
    ("mon", "09:15", "Asia/Kolkata", "2026-10-27T12:00:00Z", "2026-11-02T03:45:00Z"),
    ("sun", "03:00", "America/Los_Angeles", "2026-10-27T12:00:00Z", "2026-11-01T11:00:00Z"),  # PST by then
    # Already Sunday in Hong Kong when it's Saturday in UTC: the HK Sunday.
    ("sun", "08:00", "Asia/Hong_Kong", "2026-10-31T23:30:00Z", "2026-11-01T00:00:00Z"),
    # Spring forward: 01:30 doesn't exist in London on 29 March, nor 02:30 in
    # New York on 8 March - the first minute that does, 02:00 BST and 03:00 EDT.
    ("sun", "01:30", "Europe/London", "2026-03-27T00:00:00Z", "2026-03-29T01:00:00Z"),
    ("sun", "02:30", "America/New_York", "2026-03-06T00:00:00Z", "2026-03-08T07:00:00Z"),
    ("sun", "01:00", "Europe/London", "2026-03-27T00:00:00Z", "2026-03-29T01:00:00Z"),
    ("sun", "02:00", "Europe/London", "2026-03-27T00:00:00Z", "2026-03-29T01:00:00Z"),
    # Fall back: 01:30 comes twice - the first, still on summer time.
    ("sun", "01:30", "Europe/London", "2026-10-23T00:00:00Z", "2026-10-25T00:30:00Z"),
    ("sun", "01:30", "America/New_York", "2026-10-30T00:00:00Z", "2026-11-01T05:30:00Z"),
    # And the week after either, an ordinary week again.
    ("sun", "01:30", "Europe/London", "2026-03-29T02:00:00Z", "2026-04-05T00:30:00Z"),
    ("sun", "01:30", "Europe/London", "2026-10-25T01:00:00Z", "2026-11-01T01:30:00Z"),
])
def test_the_next_run(day, time, zone, now, expected):
    schedule = {"day": day, "time": time, "timezone": zone, "mode": "check"}
    assert admin_ops.iso(admin_ops.next_run(schedule, ts(now))) == expected


def test_the_latest_due_time(admin_env):
    sun = {**DEFAULT, "timezone": "Asia/Hong_Kong"}
    assert admin_ops.iso(admin_ops.last_due(sun, ts("2026-10-27T12:00:00Z"))) == "2026-10-24T19:00:00Z"
    assert admin_ops.iso(admin_ops.last_due(sun, ts("2026-10-31T19:00:00Z"))) == "2026-10-31T19:00:00Z"  # at it
    assert admin_ops.iso(admin_ops.last_due(sun, ts("2026-10-31T18:59:59Z"))) == "2026-10-24T19:00:00Z"


def test_saving_a_schedule_gives_its_next_run_and_shows_it(logged_in, admin_env):
    env = admin_env
    r = logged_in.put("/api/schedule", json={**DEFAULT, "timezone": "Asia/Hong_Kong"}, headers=H)
    assert r.get_json()["next_run"] == "2026-10-31T19:00:00Z"
    assert r.get_json()["next_run_local"] == "Sun 1 Nov, 03:00"
    r = logged_in.put("/api/schedule", json={**DEFAULT, "day": "wed", "time": "06:30"}, headers=H)
    assert r.get_json()["next_run"] == "2026-10-28T06:30:00Z"
    assert logged_in.get("/api/status").get_json()["schedule"]["next_run"] == "2026-10-28T06:30:00Z"
    # Saved a minute after a due time: its first one after now, next week's.
    at(env, "2026-10-28T06:31:00Z")
    r = logged_in.put("/api/schedule", json={**DEFAULT, "day": "wed", "time": "06:30", "mode": "update"}, headers=H)
    assert r.get_json()["next_run"] == "2026-11-04T06:30:00Z"


def test_a_saved_schedule_never_sets_off_a_catch_up(admin_env):
    env = admin_env
    scheduled(env)  # Sunday 03:00 UTC; the last, 25 October, handled
    at(env, "2026-10-29T12:00:00Z")  # Thursday
    env.admin.save_schedule({**DEFAULT, "day": "wed"})  # Wednesday 03:00 is gone this week: not missed
    assert state(env)["last_due"] == "2026-10-28T03:00:00Z"
    assert env.admin.tick() is None
    assert env.fake.calls == [] and runs(env) == []
    assert env.admin.schedule_view()["next_run"] == "2026-11-04T03:00:00Z"
    assert tick_at(env, "2026-11-04T03:00:00Z") == "ok"


# --- 3. due -------------------------------------------------------------------------------------


def test_the_tick_at_the_due_minute_starts_the_run_and_never_twice(admin_env):
    env = admin_env
    # The first tick after SU1 arrives notes the latest due time, and runs nothing.
    assert env.admin.tick() is None
    assert state(env) == {"last_due": "2026-10-25T03:00:00Z", "last_run": None}
    assert env.fake.calls == [] and env.admin.history() == []

    for stamp in ("2026-10-28T03:00:00Z", "2026-11-01T02:00:00Z", "2026-11-01T02:59:59Z"):
        assert tick_at(env, stamp) is None
    assert env.fake.calls == [] and env.admin.history() == []

    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    assert ["git", "fetch", "origin", "main"] in env.fake.commands()
    [run] = runs(env)
    assert run["id"] == "20261101T030000Z-scheduled_run" and run["due"] == "2026-11-01T03:00:00Z"
    assert state(env)["last_due"] == "2026-11-01T03:00:00Z"
    assert state(env)["last_run"]["id"] == run["id"]
    env.fake.clear()

    # The same due time never again: the next minute, an hour on, or after a restart.
    for stamp in ("2026-11-01T03:00:30Z", "2026-11-01T03:01:00Z", "2026-11-01T04:30:00Z"):
        assert tick_at(env, stamp) is None
    env.admin = env.make()
    assert env.admin.tick() is None
    assert env.fake.calls == [] and len(runs(env)) == 1

    # Next week's.
    assert tick_at(env, "2026-11-08T03:00:00Z") == "ok"
    assert len(runs(env)) == 2


def test_the_due_time_is_in_the_schedules_timezone(admin_env):
    env = admin_env
    scheduled(env, timezone="Asia/Hong_Kong")
    assert tick_at(env, "2026-10-31T18:59:00Z") is None
    assert tick_at(env, "2026-10-31T19:00:00Z") == "ok"  # Sunday 03:00 in Hong Kong
    assert tick_at(env, "2026-11-01T03:00:00Z") is None  # UTC's 03:00: not this schedule's


# --- 4. missed runs ---------------------------------------------------------------------------


def test_a_run_that_starts_within_the_hour_runs_as_normal(admin_env):
    env = admin_env
    scheduled(env, mode="update")
    apps_pending(env)
    at(env, "2026-11-01T03:59:00Z")
    env.admin = env.make()  # the Admin was down at 03:00, and is back 59 minutes on
    assert env.admin.tick() == "ok"
    [run] = runs(env)
    assert [s["kind"] for s in run["steps"]] == ["check", "update_apps"]
    assert history(env, "update_apps")[0]["outcome"] == "ok"
    assert run["summary"] == "Updated the apps."
    assert not any("late" in t for t in texts(run))


def test_a_late_check_only_run_checks(admin_env):
    env = admin_env
    scheduled(env)
    at(env, "2026-11-01T05:00:00Z")
    env.admin = env.make()  # down at 03:00, back at 05:00
    assert env.admin.tick() == "ok"
    [run] = runs(env)
    assert ["git", "fetch", "origin", "main"] in env.fake.commands()
    assert run["outcome"] == "ok" and run["steps"] == [{"kind": "check", "op_id": None, "outcome": "ok"}]
    assert texts(run)[0] == "This run is late: it was due at 03:00 (UTC), when the Admin wasn't running."
    assert env.admin.last_check()["checked_at"] == "2026-11-01T05:00:00Z"


def test_a_late_update_run_only_checks_and_is_a_missed_update(admin_env):
    env = admin_env
    scheduled(env, mode="update")
    apps_pending(env)
    openfit_pending(env)
    head = env.repos.head()
    at(env, "2026-11-01T05:00:00Z")
    env.admin = env.make()
    assert env.admin.tick() == "missed_update"
    [run] = runs(env)
    assert run["outcome"] == "missed_update"
    assert run["summary"] == ("The update was skipped: the Admin wasn't running at 03:00. "
                              "Next week's run will catch up.")
    # The check, and nothing else.
    assert ["git", "fetch", "origin", "main"] in env.fake.commands()
    assert all(c[0] == "git" and c[1] in READ_ONLY_GIT for c in env.fake.commands()), env.fake.commands()
    assert env.repos.head() == head and env.admin.history() == [run]
    assert run["steps"] == [{"kind": "check", "op_id": None, "outcome": "ok"}]
    assert env.admin.last_check()["behind"] == 2
    last = state(env)["last_run"]
    assert last["outcome"] == "missed_update" and last["summary"] == run["summary"] and last["dismissed"] is False
    # Once a due time: not again today, and next week's catches up.
    assert tick_at(env, "2026-11-01T05:01:00Z") is None
    assert tick_at(env, "2026-11-08T03:00:00Z") == "ok"
    assert [s["kind"] for s in runs(env)[-1]["steps"]] == ["check", "update"]


@pytest.mark.parametrize("freed, mode, outcome", [
    ("2026-11-01T03:30:00Z", "update", "ok"),  # within the hour: as normal
    ("2026-11-01T04:30:00Z", "update", "missed_update"),  # later: the check alone
    ("2026-11-01T04:30:00Z", "check", "ok"),
])
def test_a_run_blocked_by_the_lock_catches_up_when_it_frees(admin_env, capsys, freed, mode, outcome):
    env = admin_env
    scheduled(env, mode=mode)
    apps_pending(env)
    assert env.admin.lock.acquire(blocking=False)  # an operation, going at 03:00
    try:
        assert tick_at(env, "2026-11-01T03:00:00Z") == "waiting"
        assert tick_at(env, "2026-11-01T03:01:00Z") == "waiting"
        assert env.fake.calls == [] and runs(env) == []
    finally:
        env.admin.lock.release()
    said = capsys.readouterr().out
    assert said.count("[admin] schedule: the run due at 2026-11-01T03:00:00Z waits: something else is running") == 1
    assert tick_at(env, freed) == outcome
    [run] = runs(env)
    assert run["due"] == "2026-11-01T03:00:00Z"
    if outcome == "missed_update":
        assert run["summary"] == ("The update was skipped: something else was running at 03:00. "
                                  "Next week's run will catch up.")
        assert history(env, "update_apps") == []
    elif mode == "update":
        assert history(env, "update_apps")[0]["outcome"] == "ok"
    # Caught up once: not again.
    assert tick_at(env, "2026-11-01T05:00:00Z") is None
    assert len(runs(env)) == 1


def test_a_run_missed_for_weeks_catches_up_once(admin_env):
    env = admin_env
    scheduled(env)
    at(env, "2026-11-20T12:00:00Z")  # three Sundays gone
    env.admin = env.make()
    assert env.admin.tick() == "ok"
    assert [r["due"] for r in runs(env)] == ["2026-11-15T03:00:00Z"]
    assert env.admin.tick() is None


# --- 5. a check-only run --------------------------------------------------------------------


def test_a_check_only_run_checks_and_does_nothing_else(admin_env):
    env = admin_env
    connected(env)
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    openfit_pending(env)
    admin_pending(env)
    scheduled(env)
    head, shelf = env.repos.head(), (env.library / "index.json").read_text()
    clone = env.data / "repos" / AVI_SLUG / "repo"
    clone_head = admin_ops.Runner(str(clone)).run(["git", "rev-parse", "HEAD"], cwd=str(clone))[1]

    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"

    commands = env.fake.commands()
    assert ["git", "fetch", "origin", "main"] in commands and ["git", "fetch", "origin"] in commands
    assert all(c[0] == "git" and c[1] in READ_ONLY_GIT for c in commands), commands
    for word in ("merge", "reset", "build", "compose", "up", "stop", "tag", "run"):
        assert not any(word in c[1:2] for c in commands), word
    # Nothing moved: the code, the shelf, the repo's clone.
    assert env.repos.head() == head
    assert (env.library / "index.json").read_text() == shelf
    assert admin_ops.Runner(str(clone)).run(["git", "rev-parse", "HEAD"], cwd=str(clone))[1] == clone_head
    [run] = runs(env)
    assert [o["id"] for o in env.admin.history()][-1] == run["id"]  # no other op
    assert run["outcome"] == "ok" and run["steps"] == [{"kind": "check", "op_id": None, "outcome": "ok"}]
    assert run["summary"] == (f"Checked for updates: {OWNER}/{REPO}, OpenFit and the Admin have updates waiting. "
                              "Check only, so nothing was installed.")
    assert env.admin.last_check()["behind"] == 2


def test_a_check_only_run_with_nothing_new(admin_env):
    env = admin_env
    scheduled(env)
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    assert runs(env)[0]["summary"] == "Checked for updates: everything is up to date."


# --- 6. an update run, in order ------------------------------------------------------------


def test_the_apps_go_first_official_then_each_repo(admin_env):
    env = admin_env
    connected(env)
    connected(env, "acme", "openfit-extra", EXTRA)
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    extra = env.github.push("acme", "openfit-extra", {"walk/notes.txt": "new\n"}, "Walk notes")
    apps_pending(env)
    scheduled(env, mode="update")

    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"

    [run] = runs(env)
    after = steps_of(env, run)
    assert [o["kind"] for o in after] == ["update_apps", "repo_update", "repo_update"]
    apps, avi, acme = after
    # Begun in one second, and each its own op all the same.
    assert [o["id"] for o in [run] + after] == [
        "20261101T030000Z-scheduled_run", "20261101T030000Z-update_apps",
        "20261101T030000Z-repo_update", "20261101T030000Z-repo_update-2"]
    assert avi["repo"]["repo"] == REPO and acme["repo"]["repo"] == "openfit-extra"
    assert all(o["outcome"] == "ok" for o in [run] + after)
    assert {o["id"] for o in env.admin.history()} >= {o["id"] for o in [run] + after}
    # Run in that order, too: the official library's merge, then each repo's.
    commands = env.fake.commands()
    merges = [i for i, c in enumerate(commands) if c[:3] == ["git", "merge", "--ff-only"]]
    assert [commands[i][3] for i in merges] == ["origin/main", avi["new"]["commit"], acme["new"]["commit"]]
    assert run["steps"] == [{"kind": "check", "op_id": None, "outcome": "ok"},
                            {"kind": "update_apps", "op_id": apps["id"], "outcome": "ok"},
                            {"kind": "repo_update", "op_id": avi["id"], "outcome": "ok"},
                            {"kind": "repo_update", "op_id": acme["id"], "outcome": "ok"}]
    assert run["summary"] == f"Updated the apps, {OWNER}/{REPO} and acme/openfit-extra."
    log = texts(run)
    assert log.index("Update apps…") < log.index(f"Update {OWNER}/{REPO}…") < log.index("Update acme/openfit-extra…")
    assert "Nothing waiting for OpenFit." in log and "Nothing waiting for the Admin." in log
    assert state(env)["last_run"]["steps"] == run["steps"]
    # Each published: the official library and both repos.
    assert published(env, "openfit") == env.repos.head()
    assert published(env, "extra") == extra
    assert not any(c[:2] == ["docker", "compose"] for c in env.fake.commands())


def test_then_openfit_then_the_admin_last(admin_env):
    env = admin_env
    connected(env)
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    openfit_pending(env)
    admin_pending(env)
    scheduled(env, mode="update")

    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"

    [run] = runs(env)
    after = steps_of(env, run)
    assert [o["kind"] for o in after] == ["repo_update", "update", "admin_update"]
    avi, update, admin = after
    assert avi["outcome"] == "ok" and update["outcome"] == "ok" and admin["outcome"] == "running"
    assert admin["log"][-1]["text"] == HANDOVER
    assert run["outcome"] == "ok"
    assert run["summary"] == f"Updated {OWNER}/{REPO} and OpenFit, then the Admin updated itself: see its own entry."
    assert [(s["kind"], s["outcome"]) for s in run["steps"]] == [
        ("check", "ok"), ("repo_update", "ok"), ("update", "ok"), ("admin_update", "handed_over")]
    assert run["steps"][-1]["op_id"] == admin["id"]
    # In the commands too: the repo's merge, then OpenFit's merge and build,
    # then the Admin's build, and its helper last of all.
    commands = env.fake.commands()

    def first(pred):
        return next(i for i, c in enumerate(commands) if pred(c))

    repo_merge = first(lambda c: c[:3] == ["git", "merge", "--ff-only"] and len(c[3]) == 40)
    openfit_merge = first(lambda c: c == ["git", "merge", "--ff-only", "origin/main"])
    openfit_build = first(lambda c: c[:4] == ["docker", "compose", "-p", "openfit"] and "build" in c)
    admin_build = first(lambda c: c[:4] == ["docker", "compose", "-p", "openfit-admin"] and "build" in c)
    assert repo_merge < openfit_merge < openfit_build < admin_build
    assert commands[-1][:3] == ["docker", "run", "-d"] and "openfit-admin-swap" in commands[-1]


def test_steps_with_nothing_waiting_are_skipped(admin_env):
    env = admin_env
    openfit_pending(env)
    scheduled(env, mode="update")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    run = runs(env)[0]
    assert [s["kind"] for s in run["steps"]] == ["check", "update"]
    log = texts(run)
    assert "Nothing waiting for apps." in log and "Nothing waiting for the Admin." in log
    assert "Nothing waiting for OpenFit." not in log
    assert run["summary"] == "Updated OpenFit."


def test_a_run_with_nothing_to_update_is_up_to_date(admin_env):
    env = admin_env
    scheduled(env, mode="update")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    run = runs(env)[0]
    assert run["summary"] == "Up to date."
    assert [s["kind"] for s in run["steps"]] == ["check"]
    assert all(t in texts(run) for t in ("Nothing waiting for apps.", "Nothing waiting for OpenFit.",
                                         "Nothing waiting for the Admin."))
    assert env.admin.history() == [run]


def test_a_failed_check_ends_the_run_failed(admin_env):
    env = admin_env
    openfit_pending(env)
    scheduled(env, mode="update")
    env.fake.fail(lambda a: a[:2] == ["git", "fetch"], rc=128, err="Could not resolve host: github.com")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "failed"
    run = runs(env)[0]
    assert run["summary"] == "Stopped at the check: Couldn't reach GitHub to check for updates. Nothing was installed."
    assert run["steps"] == [{"kind": "check", "op_id": None, "outcome": "failed"}]
    assert env.admin.history() == [run]
    assert state(env)["last_run"]["outcome"] == "failed"


def test_a_step_its_own_route_would_refuse_ends_the_run_failed(admin_env):
    env = admin_env
    openfit_pending(env)
    scheduled(env, mode="update")
    with open(os.path.join(env.repos.repo, "app", "main.py"), "a") as f:
        f.write("# edited on the server\n")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "failed"
    run = runs(env)[0]
    assert run["summary"].startswith("Stopped at Update OpenFit: it didn't start: The server's copy of OpenFit has "
                                     "local changes")
    assert run["steps"][-1] == {"kind": "update", "op_id": None, "outcome": "refused"}
    assert env.admin.history() == [run]


# --- 7. stopping -------------------------------------------------------------------------------


def test_a_rolled_back_step_stops_the_run_before_the_admin(admin_env):
    env = admin_env
    # The Admin's own update is on offer already (an Update OpenFit pulled it),
    # so only the stop keeps the run from it.
    admin_pending(env)
    manual_update(env)
    assert env.admin.admin_update(env.admin.last_check())["can_self_update"] is True
    connected(env)
    new_avi = env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    openfit_pending(env)
    scheduled(env, mode="update")
    env.fake.unhealthy = {container_id("openfit", 2)}  # the run's new version doesn't come up

    assert tick_at(env, "2026-11-01T03:00:00Z") == "rolled_back"

    run = runs(env)[0]
    later = steps_of(env, run)
    assert [o["kind"] for o in later] == ["repo_update", "update"]
    assert later[1]["outcome"] == "rolled_back"
    # Nothing after it: no Admin update since the one before the run.
    assert len(history(env, "admin_update")) == 0
    assert run["outcome"] == "rolled_back"
    assert run["summary"] == ("Stopped at Update OpenFit: Updated, didn't come up, rolled back. "
                              "Earlier steps stayed done.")
    assert [(s["kind"], s["outcome"]) for s in run["steps"]] == [("check", "ok"), ("repo_update", "ok"),
                                                                ("update", "rolled_back")]
    assert not any(c[:4] == ["docker", "compose", "-p", "openfit-admin"] for c in env.fake.commands())
    # The repo's apps it published stayed published.
    assert published(env, "openfitavi") == new_avi
    assert not env.admin.lock.locked()
    assert state(env)["last_run"]["outcome"] == "rolled_back"


def test_a_failed_repo_update_stops_the_run_with_nothing_after_it(admin_env):
    env = admin_env
    connected(env)
    published_at = published(env, "openfitavi")
    work = env.github.tmp / "github-work" / OWNER / REPO  # a force-push: not a fast-forward
    subprocess.run(["git", "commit", "-q", "--amend", "-m", "Rewritten"], cwd=work, check=True)
    subprocess.run(["git", "push", "-qf", "origin", "HEAD:main"], cwd=work, check=True)
    openfit_pending(env)
    head = env.repos.head()
    scheduled(env, mode="update")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "failed"
    run = runs(env)[0]
    assert run["summary"] == f"Stopped at Update {OWNER}/{REPO}: Nothing was changed."
    assert [(s["kind"], s["outcome"]) for s in run["steps"]] == [("check", "ok"), ("repo_update", "failed")]
    assert history(env, "update") == [] and env.repos.head() == head  # OpenFit never started
    assert published(env, "openfitavi") == published_at


def test_a_repo_whose_update_cant_be_applied_is_passed_over(admin_env):
    env = admin_env
    connected(env)
    env.github.push(OWNER, REPO, {"repo.yaml": "source: openfit\nname: Mine\n"}, "Takes the official source")
    openfit_pending(env)
    scheduled(env, mode="update")
    # Its repo.yaml now refused: its update isn't offered, so the run says so and goes on.
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    run = runs(env)[0]
    assert any(t.startswith(f"{OWNER}/{REPO} has new commits that can't be applied:") for t in texts(run))
    assert [s["kind"] for s in run["steps"]] == ["check", "update"]


# --- 8. the lock --------------------------------------------------------------------------------


def test_nothing_else_starts_between_a_runs_steps(logged_in, admin_env, monkeypatch):
    env = admin_env
    connected(env)
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    openfit_pending(env)
    scheduled(env, mode="update")
    at(env, "2026-11-01T03:00:00Z")
    probes, real, clients = [], env.admin.last_check, []
    ran = threading.current_thread

    def client():
        # A client of the probe's own thread, logged in as the page is.
        if not clients:
            c = logged_in.application.test_client()
            assert c.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
            clients.append(c)
        return clients[0]

    def probing():
        # The run reads the last check between its steps (which comes next),
        # and inside them: every time, nothing else can start.
        if ran().name == "schedule-tick":
            so_far = [(st["kind"], st["outcome"]) for st in runs(env)[0]["steps"]]
            busy = []
            try:
                env.admin.check()
                busy.append(False)
            except admin_ops.Busy:
                busy.append(True)
            for route, body in (("/api/check", {}), ("/api/users", {"name": "lee", "port": 8110}),
                                ("/api/library/publish", {})):
                r = client().post(route, json=body, headers=H)
                busy.append(r.status_code == 409 and "Something else is running" in r.get_json()["error"])
            probes.append((so_far, all(busy)))
        return real()

    monkeypatch.setattr(env.admin, "last_check", probing)
    outcome = []
    t = threading.Thread(target=lambda: outcome.append(env.admin.tick()), name="schedule-tick")
    t.start()
    t.join(60)
    assert not t.is_alive(), "the run deadlocked on its own steps"
    assert probes and all(busy for _steps, busy in probes), probes
    assert outcome == ["ok"]
    # Probed between the steps: the repo's update finished, OpenFit's not begun.
    assert [("check", "ok"), ("repo_update", "ok")] in [steps for steps, _busy in probes], probes
    assert [o["kind"] for o in steps_of(env, runs(env)[0])] == ["repo_update", "update"]
    assert not env.admin.lock.locked()
    assert history(env, "add_user") == []


def test_the_run_is_what_is_going_between_steps(admin_env, monkeypatch):
    env = admin_env
    connected(env)
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    openfit_pending(env)
    scheduled(env, mode="update")
    seen, real = [], env.admin._begin_update

    def watching():
        seen.append(env.admin.current_op()["kind"])  # between the repo's step and OpenFit's
        return real()

    monkeypatch.setattr(env.admin, "_begin_update", watching)
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    assert seen == ["scheduled_run"]
    assert env.admin.current_op()["kind"] == "scheduled_run"  # the last op, once it's done


# --- 9. the Admin's step --------------------------------------------------------------------


def admin_handed_over(env):
    openfit_pending(env)
    admin_pending(env)
    scheduled(env, mode="update")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    run, admin = runs(env)[0], history(env, "admin_update")[0]
    return run, admin


def test_the_run_is_finished_ok_before_the_hand_over(admin_env):
    env = admin_env
    run, admin = admin_handed_over(env)
    summary = "Updated OpenFit, then the Admin updated itself: see its own entry."
    assert run["outcome"] == "ok" and run["summary"] == summary and run["finished_at"]
    assert admin["outcome"] == "running" and admin["log"][-1]["text"] == HANDOVER
    last = state(env)["last_run"]
    assert last["outcome"] == "ok" and last["summary"] == summary
    assert last["steps"][-1] == {"kind": "admin_update", "op_id": admin["id"], "outcome": "handed_over"}
    # The history the helper puts back on a rollback has the run done, and
    # the Admin's update handed over.
    snap = json.loads((env.data / "self-update" / admin["snapshot"] / "history.json").read_text())
    by_id = {o["id"]: o for o in snap}
    assert by_id[run["id"]]["outcome"] == "ok" and by_id[run["id"]]["summary"] == summary
    assert by_id[admin["id"]]["log"][-1]["text"] == HANDOVER
    # The run's log finished before the Admin's said it was handing over.
    assert run["log"][-1]["t"] <= admin["log"][-1]["t"]
    # The lock and the op are the hand-over's, as a button's Admin update leaves them.
    assert env.admin.lock.locked()
    assert env.admin.current_op()["id"] == admin["id"]


@pytest.mark.parametrize("unhealthy, outcome", [(set(), "ok"), ({1}, "rolled_back")])
def test_the_admin_update_finishes_under_the_admin_that_comes_up(admin_env, unhealthy, outcome):
    env = admin_env
    run, admin = admin_handed_over(env)
    env.fake.unhealthy = {container_id("openfit-admin", n) for n in unhealthy}
    stamp, op_id = env.fake.commands()[-1][-2:]
    assert op_id == admin["id"]
    assert admin_swap.swap(env.fake, env.repos.repo, str(env.data), stamp, op_id, env.clock, env.clock.sleep) == outcome
    env.fake.helper = False

    env.admin = env.make()  # the new Admin, or the old one put back

    done = history(env, "admin_update")[0]
    assert done["outcome"] == outcome and done["finished_at"]
    again = runs(env)[0]
    assert again["outcome"] == "ok" and again["summary"] == run["summary"]  # never marked interrupted
    assert state(env)["last_run"]["outcome"] == "ok"
    assert not env.admin.lock.locked()
    assert env.admin.tick() is None  # this week's is handled
    assert len(runs(env)) == 1


# --- 10. undo -------------------------------------------------------------------------------------


def test_undo_is_offered_after_a_scheduled_update_and_works(admin_env):
    env = admin_env
    old = env.repos.head()
    openfit_pending(env)
    scheduled(env, mode="update")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    update = history(env, "update")[0]
    undo = env.admin.status()["undo"]
    assert undo["id"] == update["id"]
    env.admin.start_undo()
    env.admin.wait(10)
    op = env.admin.current_op()
    assert op["kind"] == "undo" and op["outcome"] == "ok" and op["undoes"] == update["id"]
    assert env.repos.head() == old


# --- 11. the old timer ------------------------------------------------------------------------


def test_no_automatic_check_but_at_the_schedule(admin_env):
    env = admin_env
    assert not hasattr(admin_ops, "CHECK_EVERY")
    assert not hasattr(admin_ops.Admin, "check_due")
    assert admin_ops.TICK_EVERY == 60
    # Never checked, and the ticks all week: no check until Sunday's.
    assert env.admin.tick() is None
    for hour in range(1, 24 * 4 + 14):  # Tuesday 13:00 to Sunday 02:00
        env.clock.advance(3600)
        assert env.admin.tick() is None
    assert env.fake.calls == [] and env.admin.last_check() is None
    # A last check more than a week old is no reason either.
    (env.data / "last_check.json").write_text(json.dumps({"checked_at": "2026-10-20T12:00:00Z", "behind": 0,
                                                          "groups": [], "error": None}))
    assert tick_at(env, "2026-11-01T02:59:00Z") is None
    assert env.fake.calls == []
    assert tick_at(env, "2026-11-01T03:00:00Z") == "ok"
    assert env.fake.commands().count(["git", "fetch", "origin", "main"]) == 1


def test_serve_starts_the_schedule(monkeypatch, tmp_path):
    import admin_server
    started = []
    (tmp_path / "repo" / ".git").mkdir(parents=True)

    class FakeAdmin:
        def __init__(self, repo, data_dir):
            pass

        def publish_at_startup(self):
            pass

        def start_schedule(self):
            started.append(True)

    class FakeApp:
        def run(self, **kw):
            pass

    class FakeAuth:
        setup_code = None

        def __init__(self, data_dir):
            pass

    monkeypatch.setenv("OPENFIT_REPO", str(tmp_path / "repo"))
    monkeypatch.setenv("ADMIN_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(admin_server, "Admin", FakeAdmin)
    monkeypatch.setattr(admin_server, "Auth", FakeAuth)
    monkeypatch.setattr(admin_server, "load_secret_key", lambda d: b"k" * 32)
    monkeypatch.setattr(admin_server, "create_app", lambda *a: FakeApp())
    admin_server.serve()
    assert started == [True]


def test_the_schedules_thread_ticks_at_once_then_each_tick_every(admin_env, monkeypatch):
    env = admin_env
    ticks, enough = [], threading.Event()

    def tick():
        ticks.append(threading.current_thread().name)
        if len(ticks) == 3:
            enough.set()
        if len(ticks) in (1, 2):
            raise RuntimeError("a bug")  # said, and the thread goes on: even on the first

    monkeypatch.setattr(admin_ops, "TICK_EVERY", 0.01)
    monkeypatch.setattr(env.admin, "tick", tick)
    stop = env.admin.start_schedule()
    try:
        assert enough.wait(10)
    finally:
        stop.set()
    assert ticks[:3] == ["admin-schedule"] * 3


# --- the state ----------------------------------------------------------------------------------


def test_a_run_the_admin_stopped_in_is_interrupted_and_noticed(admin_env, monkeypatch):
    env = admin_env
    openfit_pending(env)
    scheduled(env, mode="update")

    def stop(*_a):  # the Admin stopped in the middle of OpenFit's update
        raise KeyboardInterrupt

    monkeypatch.setattr(env.admin, "_update", stop)
    at(env, "2026-11-01T03:00:00Z")
    with pytest.raises(KeyboardInterrupt):
        env.admin.tick()
    assert state(env)["last_run"]["outcome"] == "running"

    env.admin = env.make()
    last = state(env)["last_run"]
    assert last["outcome"] == "interrupted" and last["summary"] == "The Admin stopped while this was running."
    assert [s["outcome"] for s in last["steps"]] == ["ok", "interrupted"]
    assert runs(env)[0]["outcome"] == "interrupted"
    assert env.admin.tick() is None  # its due time was handled when it started


def test_a_bug_in_the_run_is_a_failed_run_not_a_running_one(admin_env, monkeypatch):
    env = admin_env
    scheduled(env, mode="update")
    monkeypatch.setattr(env.admin, "_waiting_updates", lambda: 1 / 0)
    assert tick_at(env, "2026-11-01T03:00:00Z") == "failed"
    run = runs(env)[0]
    assert run["summary"] == "Stopped on an unexpected error: division by zero"
    assert state(env)["last_run"]["outcome"] == "failed"
    assert not env.admin.lock.locked()


def test_dismiss_puts_the_notice_away(logged_in, admin_env):
    env = admin_env
    r = logged_in.post("/api/schedule/dismiss", json={}, headers=H)
    assert r.status_code == 409 and r.get_json()["error"] == "There's no scheduled run to dismiss."
    scheduled(env)
    env.fake.fail(lambda a: a[:2] == ["git", "fetch"], rc=128, err="no network")
    assert tick_at(env, "2026-11-01T03:00:00Z") == "failed"
    view = logged_in.get("/api/status").get_json()["schedule"]
    assert view["last_run"]["outcome"] == "failed" and view["last_run"]["dismissed"] is False

    r = logged_in.post("/api/schedule/dismiss", json={}, headers=H)

    assert r.status_code == 200 and r.get_json()["schedule"]["last_run"]["dismissed"] is True
    assert state(env)["last_run"]["dismissed"] is True
    assert logged_in.get("/api/status").get_json()["schedule"]["last_run"]["dismissed"] is True
    # The next run's is a notice of its own again, undismissed.
    assert tick_at(env, "2026-11-08T03:00:00Z") == "failed"
    assert state(env)["last_run"]["dismissed"] is False


def test_dismissing_a_run_that_is_going_is_refused(admin_env):
    env = admin_env
    (env.data / "schedule_state.json").write_text(json.dumps({"last_due": RUN["due"],
                                                              "last_run": {**RUN, "outcome": "running"}}))
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.dismiss_run()
    assert e.value.status == 409 and e.value.message == "The scheduled run is still going."


# --- 12. the page -----------------------------------------------------------------------------


def page_status(env, last_run=None, **schedule):
    s = env.admin.status()
    s["schedule"] = {**DEFAULT, "timezone": "Asia/Hong_Kong", "next_run": "2026-10-31T19:00:00Z",
                     "next_run_local": "Sun 1 Nov, 03:00", "last_run": last_run, **schedule}
    if last_run:
        s["activity"] = [{"id": last_run["id"], "kind": "scheduled_run", "outcome": last_run["outcome"],
                          "summary": last_run["summary"], "started_at": last_run["started_at"],
                          "finished_at": last_run["finished_at"], "log": [], "user": None, "repo": None,
                          "steps": last_run["steps"]}] + s["activity"]
    return s


@needs_node
def test_the_schedule_panels_controls_and_next_run(logged_in, admin_env):
    out = run_page(logged_in, page_status(admin_env), r"""
      const b = boot({});
      await settle();
      const opts = id => b.byId(id).children.map(o => [o.value, o.textContent]);
      out.days = opts('schedDay');
      out.modes = opts('schedMode');
      out.zones = opts('schedZone').map(o => o[0]);
      out.values = ['schedDay', 'schedTime', 'schedZone', 'schedMode'].map(id => b.byId(id).value);
      out.next = b.byId('nextRun').textContent;
      out.noteHidden = b.byId('schedNote').hidden;
      out.time = ['type', 'pattern', 'placeholder', 'maxlength'].map(a => b.byId('schedTime').getAttribute(a));
      // Edited: a repaint keeps the edit; saved: a PUT of the four, then the status again.
      b.byId('schedDay').value = 'sat'; b.byId('schedDay').fire('change');
      b.byId('schedTime').value = '04:30'; b.byId('schedTime').fire('input');
      b.byId('checkBtn').click();
      await settle();
      out.kept = [b.byId('schedDay').value, b.byId('schedTime').value];
      const before = b.fetches.length;
      b.byId('scheduleForm').fire('submit');
      await settle();
      out.sent = b.fetches.slice(before).map(f => [f.method, f.url, f.header, f.type, f.body]);
      out.msg = b.byId('schedMsg').textContent;
      out.after = b.byId('schedDay').value;  // saved: the status's again
    """)
    assert out["days"] == [["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"],
                           ["fri", "Friday"], ["sat", "Saturday"], ["sun", "Sunday"]]
    assert out["modes"] == [["check", "Check only"], ["update", "Check and update"]]
    assert out["zones"] == list(admin_ops.timezones())
    assert out["values"] == ["sun", "03:00", "Asia/Hong_Kong", "check"]
    assert out["next"] == "Next run: Sun 1 Nov, 03:00 (Asia/Hong_Kong)."
    assert out["noteHidden"] is True
    # HH:MM on the 24-hour clock, whatever the browser's locale (a time input shows AM/PM in some).
    assert out["time"] == ["text", "([01][0-9]|2[0-3]):[0-5][0-9]", "HH:MM", "5"]
    assert out["kept"] == ["sat", "04:30"]
    put = [s for s in out["sent"] if s[0] == "PUT"]
    assert put == [["PUT", "/api/schedule", "1", "application/json",
                    json.dumps({"day": "sat", "time": "04:30", "timezone": "Asia/Hong_Kong", "mode": "check"},
                               separators=(",", ":"))]]
    assert ["GET", "/api/status", "1", None, None] in out["sent"]
    assert out["msg"] == "Saved." and out["after"] == "sun"


@needs_node
def test_the_mode_note_shows_for_check_and_update(logged_in, admin_env):
    out = run_page(logged_in, page_status(admin_env, mode="update"), r"""
      const b = boot({});
      await settle();
      out.update = [b.byId('schedNote').hidden, b.byId('schedNote').textContent];
      b.byId('schedMode').value = 'check'; b.byId('schedMode').fire('change');
      out.check = b.byId('schedNote').hidden;
      b.byId('schedMode').value = 'update'; b.byId('schedMode').fire('change');
      out.again = b.byId('schedNote').hidden;
    """)
    assert out["update"] == [False, "Installs waiting updates in this order: apps, OpenFit, then the Admin. Each "
                                    "rolls back on failure. Runs whether or not anyone is using OpenFit."]
    assert out["check"] is True and out["again"] is False


def last_run(outcome, summary="Stopped at Update OpenFit: Updated, didn't come up, rolled back.", dismissed=False):
    return {**RUN, "outcome": outcome, "summary": summary, "dismissed": dismissed,
            "steps": [{"kind": "check", "op_id": None, "outcome": "ok"},
                      {"kind": "update", "op_id": "20261101T030000Z-update", "outcome": outcome}]}


@needs_node
def test_the_last_run_line_links_to_its_entry(logged_in, admin_env):
    status = page_status(admin_env, last_run(
        "ok", summary="Updated OpenFit."))
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      out.line = b.byId('lastRunLine').textContent;
      const link = find(b.byId('lastRunLine'), n => n.tagName === 'BUTTON')[0];
      out.run = link.dataset.run;
      out.before = row(b, 'activityRows', link.dataset.run).className;
      link.click();
      const item = row(b, 'activityRows', link.dataset.run);
      out.after = item.className;
      out.panel = item.children[1].textContent;
      out.steps = find(item, n => n.tagName === 'BUTTON' && n.className === 'btn sm').map(n => n.textContent);
      const none = boot({}, Object.assign({}, b.page.status, { schedule: Object.assign({}, b.page.status.schedule, { last_run: null }) }));
      await settle();
      out.none = none.byId('lastRunLine').textContent;
    """)
    assert out["line"].startswith("Last scheduled run:") and out["line"].endswith(", done")
    assert "1 Nov 2026" in out["line"]
    assert out["run"] == RUN["id"]
    assert "open" not in out["before"].split() and "open" in out["after"].split()
    assert "Updated OpenFit." in out["panel"]
    assert out["steps"] == ["Update OpenFit"]  # the run's steps, each its own entry
    assert out["none"] == "Last scheduled run: none yet."


@needs_node
@pytest.mark.parametrize("outcome, title", [
    ("failed", "The scheduled run failed"),
    ("rolled_back", "The scheduled update was rolled back"),
    ("interrupted", "The scheduled run was interrupted"),
    ("missed_update", "The scheduled update was missed"),
])
def test_a_failed_or_missed_run_puts_up_the_notice_and_dismiss_puts_it_away(logged_in, admin_env, outcome, title):
    status = page_status(admin_env, last_run(outcome))
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      out.shown = !b.byId('runNotice').hidden;
      out.title = b.byId('noticeTitle').textContent;
      out.text = b.byId('noticeText').textContent;
      b.byId('noticeOpen').click();
      out.opened = row(b, 'activityRows', b.page.status.schedule.last_run.id).className;
      b.page.status.schedule.last_run.dismissed = true;
      b.byId('noticeDismiss').click();
      await settle();
      out.posts = posts(b);
      out.after = b.byId('runNotice').hidden;
    """)
    assert out["shown"] is True and out["title"] == title
    assert out["text"].endswith(": Stopped at Update OpenFit: Updated, didn't come up, rolled back.")
    assert "open" in out["opened"].split()
    assert out["posts"] == [["/api/schedule/dismiss", "1", "{}"]]
    assert out["after"] is True


@needs_node
@pytest.mark.parametrize("run", [None, last_run("ok"), last_run("running"), last_run("failed", dismissed=True)])
def test_no_notice_for_a_run_that_worked_is_going_or_was_dismissed(logged_in, admin_env, run):
    out = run_page(logged_in, page_status(admin_env, run), r"""
      const b = boot({});
      await settle();
      out.hidden = b.byId('runNotice').hidden;
    """)
    assert out["hidden"] is True


@needs_node
def test_the_schedules_text_is_all_text_never_markup(logged_in, admin_env):
    evil = '<img src=x onerror="alert(1)"><b>bold</b>'
    run = {**last_run("failed", summary=evil), "id": RUN["id"]}
    status = page_status(admin_env, run, next_run_local=evil, timezone=evil)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      out.html = ['runNotice', 'noticeTitle', 'noticeText', 'lastRunLine', 'nextRun', 'schedDay', 'schedZone',
                  'schedMode', 'schedNote', 'activityRows'].map(id => html(b.byId(id)));
      out.text = b.byId('noticeText').textContent;
      out.next = b.byId('nextRun').textContent;
    """)
    assert out["html"] == [""] * 10
    assert out["text"].endswith(evil) and evil in out["next"]


def test_the_page_sets_the_schedules_text_with_textcontent(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    script = html.split("<script>\n", 1)[1]
    for fn in ("fillSchedule", "paintSchedule", "paintNotice", "paintLastRun"):
        body = script.split(f"function {fn}(", 1)[1].split("\n}\n", 1)[0]
        assert "innerHTML" not in body, fn


# --- 13. the routes' session and header ----------------------------------------------------


def test_the_schedules_routes_need_a_session(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)  # a password, but no session
    r = admin_client.put("/api/schedule", json={**DEFAULT, "day": "mon"}, headers=H)
    assert r.status_code == 401 and r.get_json() == {"ok": False, "error": "Log in first."}
    r = admin_client.post("/api/schedule/dismiss", json={}, headers=H)
    assert r.status_code == 401
    assert not (admin_env.data / "schedule.json").exists()
    assert not (admin_env.data / "schedule_state.json").exists()


def test_the_schedules_routes_need_the_header_and_json(logged_in, admin_env):
    for headers in ({}, {"X-OpenFit-Admin": "0"}):
        assert logged_in.put("/api/schedule", json={**DEFAULT, "day": "mon"}, headers=headers).status_code == 403
        assert logged_in.post("/api/schedule/dismiss", json={}, headers=headers).status_code == 403
    # The header, but a form, not JSON.
    assert logged_in.put("/api/schedule", data={**DEFAULT, "day": "mon"}, headers=H).status_code == 403
    assert logged_in.post("/api/schedule/dismiss", data={"x": "1"}, headers=H).status_code == 403
    assert not (admin_env.data / "schedule.json").exists()
    assert logged_in.get("/api/status").status_code == 200  # still logged in
