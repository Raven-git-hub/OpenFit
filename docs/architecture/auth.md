# Signing in to OpenFit (U1): one account per container, set through setup links

_Built: U1, the user login in [distribution.md](distribution.md)'s build
order. The code is `app/auth.py`, `app/auth_cli.py`, the guard and the
`/api/auth/*` routes in `app/main.py`, `app/templates/signin.html`, the
page's `apiFetch()`, and the Admin's Setup link. The tests are
`tests/test_auth.py`, the page's in `tests/test_routes.py`, and
`tests/test_admin_setup_link.py`._

Until U1 every OpenFit was open to anyone on the network who knew its port.
That ruled out a second real person, and reaching it from away (Tailscale).
Now each OpenFit has **one account with a password**. It's still not
row-level multi-tenancy: there's no `user_id`, and it's one person per
container and database. The Admin has its own, separate login
([admin.md](admin.md)).

There's no email. A password is set through a **one-time setup link** the
Admin issues, and the same link is how a forgotten password is reset.

## Locked from the first start

An OpenFit with no password set shows only a locked page, and every API
route but the webhook returns 401. The locked page says: "This OpenFit is
locked until a password is set. Ask your admin for a setup link." There is
no window where it's open, on the first start after the update or ever: an
`auth.json` that's missing, or won't read as the shape below, is locked too.

## The files

Both on the data volume, beside the database, and both 0600:

| File | What |
| --- | --- |
| `/data/auth.json` | `{password_hash, session_tag, setup: {token_hash, expires_at} \| null}`. Never in the `settings` table, which `/api/settings/<key>` reads and writes by any key. |
| `/data/.session_key` | Flask's secret key for the session cookies, 32 random bytes, made on the first request. Separate from `.secret_key` (`crypto.py`), which unlocks the stored device credentials. |

- `password_hash` is werkzeug's `generate_password_hash`, checked with
  `check_password_hash`. A password is 12 to 1024 characters.
- `session_tag` is `secrets.token_hex(16)`, renewed every time a password is
  set. Every session carries it, so setting a new password signs out every
  other browser.
- `auth.json` is written atomically: a 0600 temp file beside it, then
  `os.replace`. Every read-modify-write holds an `flock` on
  `auth.json.lock`, so the CLI making a link (one process) and the server
  setting a password (another) never lose each other's write, and a token is
  used once even by two requests at the same moment.
- The paths come from `app.config["AUTH_PATH"]` and
  `app.config["SESSION_KEY_PATH"]` (`$OPENFIT_AUTH_PATH` and
  `$OPENFIT_SESSION_KEY_PATH`, defaulting to the two above). The tests point
  them at `tmp_path`.

## Setup links

`python auth_cli.py setup-link`, run in the container (the image's working
folder is `/app`), is the only way a token is made. There's no HTTP route
that issues one. It:

1. makes `secrets.token_urlsafe(32)` (43 characters);
2. stores its SHA-256 and an expiry 24 hours on, replacing any earlier
   unused token;
3. prints one JSON line: `{"token": "...", "expires_at": "2026-10-29T07:00:00Z"}`.

The link is `http://<host>:<port>/setup#<token>`. The token sits in the
fragment, which a browser never sends, so it never reaches a server log.

The setup page (`GET /setup`) reads the fragment and asks for the password
twice. `POST /api/auth/setup {token, password}`:

- the token must match the stored hash (`hmac.compare_digest`) and not have
  expired, else 403 "This setup link is invalid or has expired. Ask your
  admin for a new one.";
- a password under 12 characters (or over 1024) is a 400, and the token is
  kept, so the same link can be tried again;
- on success the password is set, `setup` is cleared (single use),
  `session_tag` is renewed, and this browser is signed in. The page goes to
  `/` with `location.replace`, so the link's address doesn't stay in the
  tab's history.

The same flow works whether a password is set or not: that's the reset path.

### Making one

- **From the Admin:** the **Setup link** button on a user's row (below).
- **Without the Admin**, on the server:

  ```
  docker exec openfit-tracker-1 python auth_cli.py setup-link
  ```

  (an added user's container is `openfit-<name>-tracker-1`), then open
  `http://<server IP>:<port>/setup#<token>` with the token it printed.

On a start with no password, the server's log says so, with that command.

## Signing in and out

- `GET /` is the app when signed in; the sign-in page when a password is set;
  the locked page when none is. All three are 200 with no redirect, so the
  Admin's health check (a 200 from `/`) is unchanged. `/` and `/setup` are
  sent `Cache-Control: no-store`, so a browser never shows the app from its
  cache after signing out, nor the other way round.
- `POST /api/auth/login {password}`: 200 and a session, or 401 "That
  password isn't right.". Five failures from one address in five minutes and
  the next is a 429 until the oldest is five minutes old; the count is in
  memory (`auth.Fails`), as the Admin's is, so a restart forgets it. 409
  while locked.
- `POST /api/auth/logout` ends the session.
- `GET /api/auth/status` → `{signed_in, password_set}`.

The three views are one small template, `app/templates/signin.html`: the
same tokens and font stacks as `index.html`, light and dark (the OS's, or
the theme the app's toggle stored in this browser), no external requests.

## The guard

A `before_request` guard (`guard()` in `app/main.py`) requires a valid
session - one carrying the current `session_tag` - on every route, with
these exceptions only:

- `GET /`, `GET /setup`, `GET /api/auth/status`, `POST /api/auth/login`,
  `POST /api/auth/setup` (`OPEN_ENDPOINTS`);
- `POST /api/webhook/` and `POST /api/webhook/<token>`: devices push readings
  here with their own webhook token, as before.

The sign-in template needs no static files. A request outside those, signed
out, gets 401 `{"ok": false, "error": "signed out"}` - an unknown path too,
before any 404. A test sweeps `app.url_map`, every method of every rule, so
a route added later can't slip through.

## CSRF

Every request that isn't a GET, HEAD or OPTIONS must carry the header
`X-OpenFit: 1`, or it's a 403 - signed in or not, login and setup included.
The webhook routes are exempt: a device has no page. Another site's form
can't set a header, and its script can't without a CORS preflight OpenFit
never answers. `SameSite=Lax` keeps the cookie off another site's posts as
well.

The page sends every request through one wrapper, `apiFetch(url, opts)`:
the one place it calls `fetch`. It adds the header to every request that
isn't a GET or HEAD and goes to this origin, and keeps the caller's own
headers. `getJSON`, `fetchJSON`, `postJSON`, `send` and `sendForm` all use
it, and a test checks `fetch(` appears in `index.html` only inside
`apiFetch`. A 401 on any call while the page is open - the session ran out,
or the password was changed in another browser - reloads the page, which is
then the sign-in.

The settings popup ends with **Sign out**: it calls logout, then reloads.

## The cookie

`openfit_session_<port>` - `openfit_session_8080`: `HttpOnly`,
`SameSite=Lax`, permanent for 30 days and renewed on every request (so it
lapses after a month unused), signed with the key in `/data/.session_key`.

**No `Secure` flag**, on purpose: OpenFit is plain HTTP on your LAN, and a
browser never sends a `Secure` cookie back over `http://`, so with it nobody
could stay signed in. From away, Tailscale encrypts the traffic in transit.
HTTPS and the `.local` names are later work. On a network you don't trust,
the cookie (like the password, as you type it) is readable in transit, the
same as any plain-HTTP login.

**Named by the port** (T1). A browser keeps cookies apart by host, not by
port: one cookie of a name for `192.168.1.127`, whatever the port. With one
name for every OpenFit on a server, one browser signed in to two of them
(say `:8080` and `:8101`) had a single cookie between them, so signing in
to one replaced the other's and signed it out. Each OpenFit now names its
cookie by the port the request came to, so they sit side by side and both
stay signed in. It was never let in to the wrong one, before or since: each
container signs with its own key.

The name is `_KeyFileSessions.get_cookie_name()` in `app/main.py`: inside a
request, `openfit_session_` and the digits after the last `:` of the
request's host (`request.host`), 1 to 5 of them, else `80` - no port (as
for port 80 itself, which the host leaves out), or one that isn't one
(`a:b:c`, `x:99999999`). Outside a request it's plain `openfit_session`.
Nothing else about the cookie changed.

Rolling it out signs everyone out once: the cookie a browser holds is under
the old name, `openfit_session`, which nothing reads any more (it lapses by
itself within 30 days). Sign in again, once per OpenFit.

## The Admin's Setup link

A **Setup link** button on each user's row, usable while their OpenFit is
running. `POST /api/users/<name>/setup-link`:

1. finds the user's container by its compose labels (the Admin's usual lane);
2. runs `docker exec <id> python auth_cli.py setup-link`, an exact argument
   list, no shell, nothing variable but the checked container id;
3. parses the one JSON line, the token held to `[A-Za-z0-9_-]{40,50}` and the
   expiry to a UTC stamp;
4. returns `{link, expires_at}`, the link built from the request's host (as
   the Admin builds a user's address) and the user's port.

Not running is a 409; a failed exec a 502 with its error output (never what
it printed); an answer that isn't one well-formed line a 502. It takes the
Admin's one-at-a-time lock, so while an update or another operation runs
it's refused ("Something else is running…", 409).

The token is never written to the Admin's history, settings or log: the log
line is "Made a setup link for <name>." The page shows the link once, in a
dialog with a Copy button and "Send this to <name>. It works once, for 24
hours. A new link replaces this one.", and empties it when the dialog
closes. Nothing is stored. (The clipboard API needs HTTPS, so on the LAN
Copy falls back to copying the selected text.)

In a user's container the Admin execs only two things: the health check
and exactly `python auth_cli.py setup-link`; the Admin's own health check
runs in its own container, by the helper. The lane test holds every
scenario to that.

## Rolling it out

1. **Check now, then Update OpenFit.** Every OpenFit is now locked.
2. **Update the Admin** (U1 changes it too). It now has a Setup link button.
3. **Setup link** on your row, open it, and set your password. Do the same
   for each user, and send them theirs.

Between steps 1 and 3 OpenFit shows "locked" for a few minutes. If the Admin
update ever fails, the fallback is `docker exec openfit-tracker-1 python
auth_cli.py setup-link`, then open `http://<server IP>:8080/setup#<token>`.

## Not in U1

Two-factor sign-in, passkeys, email, and more than one account per
container; HTTPS and the `.local` names; a styled Admin page.
