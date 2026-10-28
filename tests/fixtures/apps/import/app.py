"""Import weigh-ins — load past weigh-ins from a plain-text file.

The file picker is OpenFit's; the format is this app's. Save hands
handle() the file picked as a FileUpload, and the host never reads it:
this app decodes it and parses it, a weigh-in a line -

    2026-01-03 81.2
    2026-01-04,80.9

a day (YYYY-MM-DD) and a weight in kg, apart by spaces or a comma; a
blank line is skipped. Each is filed on its own day with
ctx.write_metric(), under the app's own source, app:import - a day
already there replaced, and a day twice in the file its last line.

All or nothing: a line that won't do refuses the whole file, nothing
written - the host rolls back anything already filed.
"""

from datetime import datetime

FORMAT = "expected 'YYYY-MM-DD <kg>'"


def render(ctx, cfg):
    picker = ctx.input("file", "import", label="Import weigh-ins", kind="file", accept=".txt,.csv")
    data = ctx.metric("weight_kg")
    if not data:
        return ctx.panel(ctx.empty("No weigh-ins yet"), picker)
    return ctx.panel(ctx.number(len(data), unit="days", caption=f"since {data[0].date}"), picker)


def _weigh_ins(text):
    """(line number, day, kg) for each line of `text`, or ValueError naming the first that won't do."""
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        fields = line.replace(",", " ").split()
        if not fields:
            continue
        try:
            day, kg = fields
            datetime.strptime(day, "%Y-%m-%d")
            rows.append((n, day, float(kg)))
        except ValueError:
            raise ValueError(f"line {n}: {FORMAT}") from None
    return rows


def handle(ctx, action, payload):
    if action != "import":
        raise ValueError(f"unknown action: {action!r}")
    upload = payload.get("file")
    if not hasattr(upload, "text"):
        raise ValueError("Choose a file of weigh-ins to import")
    try:
        # utf-8-sig: a spreadsheet's export may start with a byte-order mark.
        text = upload.text("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{upload.name} isn't a text file: {FORMAT} on each line") from None
    rows = _weigh_ins(text)
    if not rows:
        raise ValueError(f"No weigh-ins in {upload.name}: {FORMAT} on each line")
    for n, day, kg in rows:
        try:
            # The host holds each to weight_kg's plausible range.
            ctx.write_metric("weight_kg", kg, date=day)
        except ValueError as e:
            raise ValueError(f"line {n}: {e}") from None
