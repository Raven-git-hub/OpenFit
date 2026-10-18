"""Workout log — log a workout just finished: a session, not a metric.

A workout is an interval - when it started and when it ended - so the app
files it as a session of OpenFit's own kind, workout, with
ctx.write_session(). Its Save sends the "log" action to handle() with
how many minutes it lasted; the workout is taken to have ended now, and
started that long before. The host files it under this app's own
source, app:workout-log, beside the workouts a watch syncs.

render() doesn't read the workouts back - an app can't read sessions
yet - so it's only the field.
"""

import math
from datetime import datetime, timedelta, timezone


def render(ctx, cfg):
    return ctx.panel(
        ctx.empty("Log a workout with Save"),
        ctx.input("minutes", "log", label="Workout", unit="min"),
    )


def handle(ctx, action, payload):
    if action != "log":
        raise ValueError(f"unknown action: {action!r}")
    raw = payload.get("minutes")
    try:
        # bool is a number to Python, but true is no length of time.
        minutes = None if isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        minutes = None
    if minutes is None or not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("Enter the workout in minutes, as a positive number")
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    ctx.write_session("workout", start, end, {"type": "manual", "duration_minutes": minutes})
