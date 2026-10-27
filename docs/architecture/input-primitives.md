# App input primitives

How an app's tile takes input from the user, and the primitives it can offer.
Design record for the path dropdown → file upload → date/time, each its own
brick. Dropdown and file upload are built (below, each with its worked
example); the rest are sketched.

## Today — the `input` element

An app's `render(ctx, cfg)` returns a spec; `ctx.input(key, action, label=None,
kind="number", unit=None, options=None, accept=None)` (`app/host.py`) puts a
field and a **Save** on the tile. `INPUT_KINDS` is `("number", "text",
"select", "file")`. The browser (`specInput`, `app/templates/index.html`) draws
the field; Save (or Enter) POSTs `{action, payload: {key: value}, cfg}` to
`/api/app/<id>/action` — a file input's the same, as multipart (below) — which
runs the app's `handle(ctx, action, payload)` and redraws the tile from the
fresh spec. Every input spec carries `options` and `accept`, each `null` but
for its own kind.

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
- **Worked example.** The `mood` app, committed in `library/mood/`
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

## File upload — kind `"file"` (built)

A file picker, for an app that takes a file — a roster's `.ics`, a `.csv`
export, a PDF, another app's SQLite database. The principle, settled with the
owner: **OpenFit facilitates the upload and never parses.** The app brings its
own parser; the host delivers the file to `handle()` and knows no format, so
the platform stays open to any file an author brings.

- **Spec.** The kind `"file"` in `INPUT_KINDS`, and
  `ctx.input(..., kind="file", accept=".ics,.csv")`. `accept` is the picker's
  hints, as an `<input type="file">` takes them (extensions, MIME types or
  both, comma-separated), optional and only for a file: non-empty text,
  checked by the builder as `label`/`unit` are — not text → `TypeError`,
  empty → `ValueError` ("a file input's accept must be non-empty text"), given
  for another kind → `ValueError` ("accept is only for a file"), mirroring
  `options`. It's a suggestion to the picker, never enforced: a user can pick
  any file, and the app checks what it got. `options` stays select-only (a
  file's is `null`). The spec carries `"accept"` (the string, or `null`).
- **Render.** `specInput` has a `file` branch: an `<input type="file"
  class="input">`, its `accept` set (as a property, never markup) when the
  spec has one, in the field's place — the label over it, the unit, Save and
  the message line as for the other kinds. The browser's own button is styled
  as the page's small buttons, the file's name beside it; in a tile too narrow
  for both and Save, Save wraps under them, at the row's end. Enter isn't
  wired (in a file picker it opens the picker): Save sends. With no file
  picked Save says "Choose a file." and sends nothing; a file over the cap
  (`UPLOAD_MAX`, the server's 25 MiB) "File too large (max 25 MB)." and
  nothing. The reply's redraw or refusal behave as for the other kinds: a
  refusal shows under the picker, the file still picked.
- **Transport.** Bytes don't fit a JSON payload, so a file input posts
  `multipart/form-data` to the same action route — the only new transport.
  The page's `api.appActionFile(id, action, key, file, cfg)` builds a
  `FormData` of `action`, `cfg` (as JSON) and the file under the input's key,
  and `sendForm()` POSTs it, leaving the `Content-Type` (and its boundary) to
  the browser; the reply is `send()`'s `{ok, ...}`. The route reads `action`
  from the form, `payload` and `cfg` from form fields holding their JSON
  (either may be left out: `{}`; a `payload` that isn't a JSON object is the
  JSON path's 400, a `cfg` that isn't is `{}`), and sets each file into
  `payload` under its field. Number, text and select inputs keep posting JSON,
  untouched. `WriteCtx` doesn't change: the file rides in `payload`.
- **Size cap.** `MAX_UPLOAD_BYTES` = 25 MiB (`app/main.py`), set as Flask's
  `MAX_CONTENT_LENGTH`: a body over it is turned away with a `413` before any
  of it is read — on every route, a platform-wide backstop, comfortably above
  anything else any route receives (an app's install is a few small text
  files). The 413 is said as any refusal is, `{"ok": false, "error": "Request
  too large (max 25 MB)."}`, so a tile shows why; the page's own check stops a
  file over the cap before it's sent.
- **The file object.** `payload[key]` is a `FileUpload` (`app/host.py`):
  - `.name` — the file's name as the browser gave it, for display only (never
    used as a path);
  - `.content_type` — the type the browser reported, `""` for none;
    informational — the app shouldn't trust it;
  - `.bytes` — the raw bytes (read from the temp file when first asked for);
  - `.text(encoding="utf-8")` — `.bytes.decode(encoding)`; a decode error
    propagates, the app's to catch (a binary file read as text is the app's
    mistake);
  - `.path` — the temp file's path, for a library that needs a real file
    (stdlib `sqlite3.connect(f.path)`).
- **Lifecycle: nothing is persisted.** Each file is written to a temp file the
  host names (`tempfile.mkstemp`, never the client's filename) for the life of
  the action, and deleted after — in a `finally`, whether `handle()`
  succeeded, refused or raised (one the app removed itself is fine). OpenFit
  doesn't become a file store: only what the app writes (metrics, through
  `ctx`) survives. Own-source only is unchanged: a write is `app:<id>`,
  through the same `WriteCtx`, its rollback-on-raise and per-day re-derive.
- **Validation.** All the app's. The host checks nothing about a file — not
  its type, its size beyond the cap, nor its content — and parses nothing,
  not even an `.ics`.
- **Worked example.** The `import` app, committed in
  `library/import/` — a bulk importer, the file equivalent of the
  Weight and Mood apps. A picker (`accept=".txt,.csv"`, "Import weigh-ins")
  under how many days of weigh-ins the dataset holds. Its `handle()` decodes
  the file itself (`f.text("utf-8-sig")`, a spreadsheet's byte-order mark
  taken; not text → its own refusal), parses a weigh-in a line, `YYYY-MM-DD
  <kg>` apart by spaces or a comma, blank lines skipped (a line that won't →
  `ValueError("line N: expected 'YYYY-MM-DD <kg>'")`, nothing written), and
  files each with `ctx.write_metric("weight_kg", kg, date=day)` — on its own
  day, not today — under `app:import`; a weight the host's plausible range
  refuses is said with its line number, the lines before it rolled back. It
  exercises everything new: a file delivered to the app, app-side parsing,
  and writing to arbitrary dates. Tests exercise the builder and the
  `FileUpload` (`tests/test_app_host.py`), the render branch and the
  multipart sender under the node/DOM harness — the page's own `FormData`,
  encoded by node, posted to the route (`tests/test_routes.py`) — and the
  multipart route and install-to-derived round trip, the temp file gone after
  every outcome and the 413 (`tests/test_app_actions.py`).

### Deferred (not this brick)
- Any parsing by OpenFit, or format support in core: apps parse.
- Per-app dependencies: an app gets the host image's Python (`sqlite3` is
  stdlib; a PDF library isn't there). Enabling an app's own libraries is a
  separate question; this brick delivers bytes.
- Persistent file storage or attachments: the file is transient.
- Binary niceties — thumbnails, type sniffing or enforcement (`.content_type`
  is informational).
- Several files in one input, multi-select, drag-and-drop.
- Session write-back (`write_session`): the roster-to-sessions app waits on
  it; the `import` app writes metrics only. (Since landed, separately:
  `ctx.write_session()`, existing kinds only — `app-host.md`; and the roster
  app on it, now FRMS, `library/frms/`.)

## A file input's toggle, and confirm-before-submit (built)

A file input can carry one companion checkbox — a choice that goes with the
file — and a confirm the page asks before sending with it ticked:

```python
ctx.input("file", "upload", label="Upload roster", kind="file", accept=".ics",
          toggle={"key": "backfill", "label": "Backfill",
                  "confirm": "Backfill past duties into your rostered plan? ..."})
```

- **Host:** `toggle` only for `kind="file"` (another kind's → `ValueError`);
  a mapping of only `key`, `label`, `confirm` — `key` and `label` non-empty
  text, `key` never the input's own (the file has it), `confirm` optional
  non-empty text. The spec carries `"toggle": {key, label, confirm}`
  (`confirm` null for none) only when given: every input without one is the
  spec from before.
- **Page:** a checkbox in its label, between the picker and Save, unticked.
  Save sends `payload` `{<toggle key>: true|false}` beside the file (a form
  field of its JSON; the route already merged one), ticked or not. Ticked,
  with a `confirm`, Save first opens the page's own small modal — the label
  over the app's text, [Cancel] / [Continue], never the browser's
  `confirm()` — and sends only on Continue. A file input without a toggle
  sends no `payload` field: the request as before.
- **App:** `handle()` reads `payload.get(<key>) is True`. The confirm is
  static — fixed at render, before the file's read — so it can't count what
  the file holds; a count-aware confirm is a later step.
- **Reference:** the roster's **Backfill** (`app-host.md`, the FRMS app).
  Tests: the builder (`tests/test_app_host.py`), the checkbox, the payload and
  the confirm under the node/DOM harness (`tests/test_routes.py`), and the
  round trip (`tests/test_app_actions.py`).

## Later bricks (sketch only)
- **Date / time** — a day or timestamp for a reading, rather than "today".
- **Auto-recompute** — re-run an app's computed output when its inputs change.

Each stays a separate brick; none is bundled with the dropdown or the file
upload.
