# App input primitives

How an app's tile takes input from the user, and the primitives it can offer.
Design record for the path dropdown → file upload → date/time, each its own
brick. Dropdown is built (below, with its worked example); the rest are
sketched.

## Today — the `input` element

An app's `render(ctx, cfg)` returns a spec; `ctx.input(key, action, label=None,
kind="number", unit=None, options=None)` (`app/host.py`) puts a field and a
**Save** on the tile. `INPUT_KINDS` is `("number", "text", "select")`. The
browser (`specInput`, `app/templates/index.html`) draws the field; Save (or
Enter) POSTs `{action, payload: {key: value}, cfg}` to `/api/app/<id>/action`,
which runs the app's `handle(ctx, action, payload)` and redraws the tile from
the fresh spec.

The split every primitive keeps: **the host carries the value, the app owns the
meaning.** `handle()` validates `payload[key]` and decides what to do with it —
the Weight app checks its number is finite and positive before
`ctx.write_metric()`. The action route does not enforce an input's own
constraints, and a value never names a source: the write is always `app:<id>`.

## Dropdown — kind `"select"` (built)

A choose-one field, for logging a value from a fixed set (a mood, a tag, a
category) rather than typing it.

- **Spec.** The kind `"select"` in `INPUT_KINDS`, and
  `ctx.input(..., kind="select", options=[...])`. `options` is a non-empty list
  (or tuple) of non-empty **strings**, checked by the builder as `label`/`unit`
  are: missing, `None` or empty → `ValueError`; not a list, or an entry that
  isn't a non-empty string → `TypeError` ("a select's options must be a
  non-empty list of strings"). It's required for a select and refused for the
  other kinds (`ValueError`, "options is only for a select"). Every input spec
  carries `"options"`: a select's as a plain list of strings, in order and as
  written; `null` for number/text.
- **Render.** `specInput` has a `select` branch: a `<select class="input">`
  with a "Choose…" placeholder first (value `""`, selected and disabled, so it
  can't be chosen back), then one `<option>` per entry, its text and value the
  string (set as text, never markup), in place of the `<input>`. Save rejects
  the unmade choice ("Choose an option.", nothing sent) the way an empty number
  is rejected, so a log is always deliberate. The label, unit, Save, Enter and
  the reply's redraw or refusal behave as for number/text.
- **Payload.** Save POSTs `{key: <the chosen string>}` — the same shape as
  number/text, a string value. `handle()` reads it, checks it is one it expects,
  and does what it likes (map it to a number, write a metric, store a tag).
- **Validation.** App-side, as for number/text. The host does **not** check the
  submitted value is one of the offered options: the action route keeps no
  memory of a past render's options, and making it stateful for this would break
  the stateless action model. An app that cares rejects an unexpected value in
  `handle()`.
- **Worked example.** The `mood` app, committed in `tests/fixtures/apps/mood/`
  (the canonical `mood` metric — Other, 1–5, so no `metrics:` block): a select
  of the options `"1"`–`"5"`, "Mood today", under the latest day's mood (or
  `empty("No mood logged yet")`). Its `handle()` takes `log` only, refuses a
  value that isn't one of its five strings ("Choose a mood from 1 to 5") and
  files the choice with `ctx.write_metric("mood", int(raw))` for today, under
  `app:mood` — the Weight app's shape, with a select instead of a number
  field. Plain-string options mean no label→value map is needed while
  `{value, label}` is deferred, and the membership check in `handle()` is the
  ownership split made visible. Tests exercise the builder
  (`tests/test_app_host.py`), the render branch under the node/DOM harness
  (`tests/test_routes.py`) and the install-to-derived round trip
  (`tests/test_app_actions.py`).

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
