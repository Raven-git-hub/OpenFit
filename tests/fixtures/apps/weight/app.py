"""Weight — log a weigh-in, and show the latest one.

render() reads the latest weight through ctx.metric() (the OpenFit access
contract): one derived, source-tagged value per day, whichever source
won it. Under it, a field to type a new weigh-in into; its Save sends the
"log" action to handle(), which files it for today with
ctx.write_metric(). The host files it under this app's own source,
app:weight - the app never names a source, so it can only ever write its
own rows.

One weigh-in a day: logging again the same day replaces the first.
"""

import math


def render(ctx, cfg):
    log = ctx.input("weight_kg", "log", label="Log weight", unit="kg")
    data = ctx.metric("weight_kg")
    if not data:
        return ctx.panel(ctx.empty("No weigh-ins yet"), log)

    latest = data[-1]
    caption = latest.date
    if latest.source:
        caption += f" · via {latest.source}"
    return ctx.panel(ctx.number(latest.value, caption=caption, unit="kg"), log)


def handle(ctx, action, payload):
    if action != "log":
        raise ValueError(f"unknown action: {action!r}")
    raw = payload.get("weight_kg")
    try:
        # bool is a number to Python, but true is no weight.
        kg = None if isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        kg = None
    if kg is None or not math.isfinite(kg) or kg <= 0:
        raise ValueError("Enter your weight in kg, as a positive number")
    ctx.write_metric("weight_kg", kg)
