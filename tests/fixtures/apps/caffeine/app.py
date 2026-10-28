"""Caffeine — log the day's caffeine: a metric this app invents.

OpenFit has no caffeine metric, so the app declares one in its app.yaml -
caffeine_mg, in mg, under Nutrition & hydration, one figure a day, 0 to
2000 - and reads and writes it by that short key. The host stores it as
app:caffeine:caffeine_mg, under this app's own source, app:caffeine, and
lists it in the catalog beside the built-in metrics. A built-in metric
would just be written, undeclared.

render() shows the latest day's figure over a field to type today's
into; its Save sends the "log" action to handle(), which files it for
today with ctx.write_metric(). One figure a day: logging again the same
day replaces it.
"""

import math


def render(ctx, cfg):
    log = ctx.input("caffeine_mg", "log", label="Caffeine today", unit="mg")
    data = ctx.metric("caffeine_mg")
    if not data:
        return ctx.panel(ctx.empty("No caffeine logged yet"), log)

    latest = data[-1]
    caption = "today" if latest.date == ctx.today() else latest.date
    return ctx.panel(ctx.number(latest.value, caption=caption, unit="mg"), log)


def handle(ctx, action, payload):
    if action != "log":
        raise ValueError(f"unknown action: {action!r}")
    raw = payload.get("caffeine_mg")
    try:
        # bool is a number to Python, but true is no amount.
        mg = None if isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        mg = None
    if mg is None or not math.isfinite(mg) or mg < 0:
        raise ValueError("Enter today's caffeine in mg, as a number")
    # The declared range (0-2000) is the host's to hold it to.
    ctx.write_metric("caffeine_mg", mg)
