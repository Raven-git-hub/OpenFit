"""Mood — log today's mood on the canonical 1-5 scale, via a dropdown."""


def render(ctx, cfg):
    log = ctx.input("mood", "log", kind="select",
                    options=["1", "2", "3", "4", "5"], label="Mood today")
    data = ctx.metric("mood")
    if not data:
        return ctx.panel(ctx.empty("No mood logged yet"), log)
    latest = data[-1]
    caption = "today" if latest.date == ctx.today() else latest.date
    return ctx.panel(ctx.number(latest.value, caption=caption), log)


def handle(ctx, action, payload):
    if action != "log":
        raise ValueError(f"unknown action: {action!r}")
    raw = payload.get("mood")
    if raw not in {"1", "2", "3", "4", "5"}:
        raise ValueError("Choose a mood from 1 to 5")
    ctx.write_metric("mood", int(raw))
