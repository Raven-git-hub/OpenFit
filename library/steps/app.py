"""Steps — display the latest daily step count plus a 7-day line.

Reads only through ctx.metric() (the OpenFit access contract), so it receives
one derived, source-tagged value per day and never touches the raw metrics
table or any vendor API.
"""


def render(ctx, cfg):
    data = ctx.metric("steps", days=7)
    if not data:
        return ctx.empty("No step data yet")

    latest = data[-1]
    caption = latest.date
    if latest.date == ctx.today():          # today is a partial day
        caption = f"{latest.date} · so far today"
    if latest.source:
        caption += f" · via {latest.source}"

    return ctx.panel(
        ctx.number(round(latest.value), label="steps", caption=caption),
        ctx.line([(p.date, p.value) for p in data], title="last 7 days"),
    )
