"""Distance (from steps) — convert the latest day's steps into metres travelled.

Two inputs: the step count (from the dataset, via the contract) and the stride
length (config, supplied by the user; defaults to 0.75 m). The stride is NOT a
metric — it's a fact that's true until changed — so the host passes it in via
cfg, and nothing is written back to the dataset: metres is computed on the fly.
"""


def render(ctx, cfg):
    data = ctx.metric("steps", days=7)
    if not data:
        return ctx.empty("No step data yet")

    latest = data[-1]
    stride = cfg.get("stride_m", 0.75)
    metres = round(latest.value * stride)

    note = "so far today" if latest.date == ctx.today() else latest.date
    return ctx.number(
        metres,
        label="metres",
        caption=f"{round(latest.value)} steps × {stride} m · {note}",
    )
