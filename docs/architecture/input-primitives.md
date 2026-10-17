# App input primitives

How an app's tile takes input from the user, and the primitives it can offer.
Design record for the path dropdown → file upload → date/time, each its own
brick. Dropdown (this slice) is specified in full; the rest are sketched.

## Today — the `input` element

An app's `render(ctx, cfg)` returns a spec; `ctx.input(key, action, label=None,
kind="number", unit=None)` (`app/host.py`) puts a field and a **Save** on the
tile. `INPUT_KINDS` is `("number", "text")`. The browser (`specInput`,
`app/templates/index.html`) draws the field; Save (or Enter) POSTs
`{action, payload: {key: value}, cfg}` to `/api/app/<id>/action`, which runs the
app's `handle(ctx, action, payload)` and redraws the tile from the fresh spec.

The split every primitive keeps: **the host carries the value, the app owns the
meaning.** `handle()` validates `payload[key]` and decides what to do with it —
the Weight app checks its number is finite and positive before
`ctx.write_metric()`. The action route does not enforce an input's own
constraints, and a value never names a source: the write is always `app:<id>`.

## Dropdown — kind `"select"` (this slice)

A choose-one field, for logging a value from a fixed set (a mood, a tag, a
category) rather than typing it.

- **Spec.** A new kind `"select"` in `INPUT_KINDS`, and
  `ctx.input(..., kind="select", options=[...])`. `options` is a non-empty list
  of **strings**, validated by the builder the way `label`/`unit` already are
  (each a non-empty string); `options` is required for a select and rejected for
  the other kinds. The emitted spec gains `"options"`.
- **Render.** `specInput` grows a `select` branch: a `<select>` with a
  non-selectable "Choose…" placeholder first, then the options, in place of the
  `<input>`. Save rejects the unmade choice the way an empty number is rejected,
  so a log is always deliberate. The unit slot and label behave as now.
- **Payload.** Save POSTs `{key: <the chosen string>}` — the same shape as
  number/text, a string value. `handle()` reads it, checks it is one it expects,
  and does what it likes (map it to a number, write a metric, store a tag).
- **Validation.** App-side, as for number/text. The host does **not** check the
  submitted value is one of the offered options: the action route keeps no
  memory of a past render's options, and making it stateful for this would break
  the stateless action model. An app that cares rejects an unexpected value in
  `handle()`.
- **Worked example.** A `mood` app (the canonical `mood` metric — Other, 1–5):
  a select of the five levels whose `handle()` maps the choice to the number and
  `ctx.write_metric("mood", …)` — the Weight app's shape, with a select instead
  of a number field. A fixture app plus tests exercise the builder, the render
  branch (the node/DOM harness) and the round trip.

### Deferred (not this brick)
- `{value, label}` options (shown text ≠ submitted value). Additive later if an
  app needs it; nothing does yet. Options stay plain strings for now.
- Host-side option-membership enforcement (see Validation).
- Multi-select. One choice only.

## Later bricks (sketch only)
- **File upload** — what the fatigue manager needs (the `.ics` roster). The
  heavy one: size limit, where the bytes live, parsing, privacy — its own
  design pass.
- **Date / time** — a day or timestamp for a reading, rather than "today".
- **Auto-recompute** — re-run an app's computed output when its inputs change.

Each stays a separate brick; none is bundled with the dropdown.
