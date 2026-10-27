# The OpenFit Admin (A1): today's update, behind buttons

_Built: A1, the first slice of the Admin in
[distribution.md](distribution.md). The code is `admin/`; the tests are
`tests/test_admin_*.py`._

Until A1, OpenFit was updated by SSHing to the docker host and running:

```
cd ~/containers/openfit && git checkout main && git pull && docker compose up -d --build
```

A1 puts exactly that behind buttons in a small web admin on port 8090, and
adds the safety nets the command line never had: a dirty-tree check, a
backup of the data before every update, a health check afterwards, an
automatic undo if the new version doesn't come up, and an Undo button.

It manages **one** OpenFit: the existing compose project, adopted in place,
with the same storage, the same port 8080 and the same compose project.
Commands run from the host still work on it exactly as before.

## What A1 doesn't do

- Add, remove or list users, or assign ports (A2).
- Update itself. Its own update is a command you run (below) until A4.
- Serve the library, Browse, organisation repos, `replaces` (L3) or
  host-version labels (L4).
- Fetch anything from GitHub but the repo itself: pull request numbers are
  read from merge commit titles and linked, never fetched.
- The proxy and `.local` names. OpenFit and the Admin are reached at
  `IP:port`.
- The user system's own login, and the backup file format. An A1 backup is
  a plain `tar.gz` of the `/data` volume.

## Install

On the docker host, from the OpenFit repo. This is the only command-line
step:

```
cd ~/containers/openfit && git pull
cp admin/.env.example admin/.env      # edit if the repo lives elsewhere
docker compose -f admin/docker-compose.yml -p openfit-admin up -d --build
docker logs openfit-admin-admin-1     # the one-time setup code
```

Then open `http://<server IP>:8090`, enter the setup code and choose a
password.

`admin/.env` holds one line, `OPENFIT_REPO=/home/docker/containers/openfit`:
the repo's absolute path on the host. The Admin's compose file mounts the repo
at that **same path** inside the container, so compose resolves the user
system's paths (its build context, its `.env`) the same inside and out, and
the containers it recreates carry the same compose labels as ones started from
the host.

The Admin's container (`admin/Dockerfile`) is `python:3.12-slim` with git,
the docker CLI (`docker:27.5.1-cli`) and the compose plugin
(`docker/compose-bin:v2.32.4`), both copied from their pinned images. It runs
as root, for the docker socket.

## Logging in

- **The setup code.** On a first start (no password yet) the Admin makes a
  setup code (`secrets.token_urlsafe(16)`), prints it once to its log and
  keeps it in memory only. A restart before setup prints a new one.
- **The password** needs at least 12 characters. The setup code and a password
  set it, stored as a werkzeug password hash in `/data/auth.json`. From then
  on `/api/setup` answers 410 for good. Setting it also logs you in.
- **Sessions** are Flask's, signed with a key made once into
  `/data/secret_key`. The cookie is named `openfit_admin` (cookies aren't kept
  apart by port, and OpenFit is on the same host), `HttpOnly` and
  `SameSite=Strict`. It is **not** `Secure`: the Admin is plain HTTP on the
  LAN. Don't expose port 8090 to the internet; reach it from away over
  Tailscale.
- **Wrong passwords.** Five failed logins from one address in five minutes,
  and the next is refused (429) until the oldest is five minutes old. The
  count is kept in memory.
- **Every `/api` route** except `setup` and `login` needs a session (401).
  **Every request that isn't a GET** must be JSON and carry the header
  `X-OpenFit-Admin: 1` (403). That's the CSRF guard: another site's page
  can't send either. The page's `fetch` wrapper always sends the header.
- **Forgotten password:** delete `auth.json` from the Admin's volume and
  restart the Admin. It prints a new setup code. A new password ends every
  session made under the old one.

## Checking for updates

A check is `git fetch origin main`, and nothing else: the working tree and
`HEAD` are never touched. It reports:

- `current`: `HEAD`'s short sha and subject.
- `behind`: how many commits `origin/main` is ahead.
- `groups`: the changes, by top-level folder, from
  `git diff --name-only HEAD origin/main`:
  - **User system**: `app/`
  - **Admin**: `admin/`
  - **App library**: `library/`
  - **Other**: anything else, such as docs and tests.

  Each group lists its commits (`git log --no-merges --format=%h%x09%s
  HEAD..origin/main -- <folder>`). "Other" lists the commits no other group
  lists. A merge commit titled `Merge pull request #N` gives a PR number,
  listed under every group its files touch, as a link to
  `https://github.com/Raven-git-hub/OpenFit/pull/N` that your browser opens.

The result is saved to `/data/last_check.json`. A check runs on **Check
now**, and automatically from a background thread: it wakes hourly and checks
once a week has passed since `checked_at`. An automatic check only fetches;
it never updates. A check that fails (no network, say) keeps the last good
result and records the error and its time, which the page shows. Because
`checked_at` is the last *successful* check, a failing automatic check is
retried hourly until one succeeds.

## Updating OpenFit

**Update OpenFit** is offered when the last check shows anything new
(pulling brings everything, so it's one button). It asks first: "This stops
OpenFit for about a minute and backs up your data first." One operation runs
at a time (an update, an undo or a check), and a second request gets 409.

Steps 1 and the branch check run in the request, so a refusal is an
immediate 409 with nothing changed. The rest runs in a background thread,
in this order:

1. **Clean?** `git status --porcelain`, and any output refuses: "The
   server's copy of OpenFit has local changes: `<files>`. Nothing was
   changed." No later step runs. The repo must also be on `main`, or the
   update is refused the same way.
2. **Remember.** `HEAD` is noted as `old`. The user system's container is
   found by its compose labels, and its image is tagged
   `openfit-tracker:prev`.
3. **Pull.** `git merge --ff-only origin/main`. If it can't fast-forward,
   it stops: nothing changed.
4. **Build.** `docker compose … build tracker`. OpenFit is still running the
   old version. If the build fails, `git reset --hard <old>`, report, and
   stop. The container was never stopped.
5. **Stop.** `docker compose … stop tracker`.
6. **Back up.** The volume mounted at `/data` is read from the stopped
   container (`docker inspect`, never hard-coded), and a helper container
   archives it:
   `docker run --rm --label openfit.admin.helper=true -v <volume>:/v:ro -v openfit-admin_admin-data:/b openfit-admin:local tar czf /b/backups/<UTC timestamp>-<old sha>.tar.gz -C /v .`
   If the backup fails, the code goes back (`git reset --hard <old>`), the
   old image is put back as `openfit-tracker:latest` (the build has already
   replaced it), and `up -d --no-build` starts OpenFit on the old version
   again. Report, and stop.
7. **Start.** `docker compose … up -d --no-build tracker`.
8. **Health check.** The container is found again (compose has made a new
   one), and `docker exec <container> python -c "<urlopen
   http://127.0.0.1:80/, exit 0 on a 200>"` is polled every 2 s for up to
   60 s. If it's healthy, the update is done: backups are pruned to the
   newest 3. If not, the undo sequence runs automatically: "Updated, didn't
   come up, rolled back."

**The undo sequence** runs automatically for a failed health check, and is
also the **Undo last update** button. Undo is offered only after a successful
update, for that update only (the latest update or undo is that update, its
backup is still there, and the repo is still on the version it updated to).
It warns that anything recorded since the update is lost.

1. Stop the service.
2. Restore that update's backup into the volume, with a helper:
   `sh -c "find /v -mindepth 1 -delete && tar xzf /b/backups/<file> -C /v"`.
   These are fixed strings, and the file name is from the Admin's own listing,
   checked against the backup name pattern. Restoring matters because the new
   version may have migrated the database.
3. `docker tag openfit-tracker:prev openfit-tracker:latest`.
4. `git reset --hard <old>` (as the owner).
5. `docker compose … up -d --no-build --force-recreate tracker`, then the
   health check.

If the data can't be restored, OpenFit is left stopped and the log names the
backup. Starting on a half-restored volume would be worse.

**Progress.** Each operation has a log of `{t, text}` lines in plain words
("Backing up your data…"), with the tail of a command's output when it
fails. `GET /api/op` returns the running operation, else the last one; the
page polls it every second while one runs. The last 20 operations are kept
in `/data/history.json`; one the Admin was stopped in the middle of reads
"interrupted" after a restart.

## The Admin's own update (until A4)

When the check's Admin group is non-empty, the page shows "An Admin update is
available", its commits, and the command to run on the server:

```
cd ~/containers/openfit && docker compose -f admin/docker-compose.yml -p openfit-admin up -d --build
```

The Admin never rebuilds or restarts itself in A1. Updating OpenFit pulls the
Admin's new files too, so after an update the note stays up, saying the files
are already on the server, until the Admin has been rebuilt. It tells by
comparing the files it's running (its image holds all of `admin/`) with the
repo's `admin/`, dotfiles and caches left out.

## The safety rules

The Admin holds the docker socket, so it has full control of the host. A
shared server runs other things (n8n, Open WebUI, Music Assistant,
Tailscale sidecars and more), and the Admin must never touch any of them.

- **Project-scoped.** Every compose command is `docker compose -p openfit -f
  <OPENFIT_REPO>/docker-compose.yml … tracker`, run in `OPENFIT_REPO`. Every
  docker command targets one of two things: the container compose labels
  `com.docker.compose.project=openfit` and
  `com.docker.compose.service=tracker` (not a one-off `compose run`), found by
  `docker ps -a` with those label filters; or a helper container the Admin
  starts itself, labelled `openfit.admin.helper=true`, always `--rm`, from its
  own image. The tags it sets are `openfit-tracker:prev` and
  `openfit-tracker:latest`. Nothing else is listed, inspected, stopped or
  removed, and nothing touches the `openfit-admin` project.
- **No shell.** Every command is an argument list, run through one `Runner`
  (`admin_ops.py`) with `subprocess.run`, never a shell. Nothing a person
  types is ever placed in a command: the arguments are fixed strings, the
  settings (checked against strict patterns when read), and ids that git
  and docker gave back (checked before use). The one `sh -c` is the restore
  helper's fixed string, inside a container. A test reads every
  `admin/*.py` for `shell=True` and `os.system`.
- **Git as the owner.** Git runs as the uid and gid that own `OPENFIT_REPO`,
  with `HOME=/tmp` and no supplementary groups, so the files `git merge`
  writes stay owned by `docker` (your own `git pull` keeps working) and git's
  "dubious ownership" refusal never fires. Docker runs as root.

## Where its data lives

The Admin's volume, `openfit-admin_admin-data` (its name fixed in the
compose file, because the backup helpers mount it by name), at `/data`:

| File | What |
| --- | --- |
| `admin.json` | The settings, written with the defaults on first start: `{"user": "andy", "project": "openfit", "service": "tracker", "port": 8080, "branch": "main"}`. No UI changes them in A1. |
| `auth.json` | The password hash (0600). |
| `secret_key` | The session key (0600). |
| `last_check.json` | The last check, and the last failed one's error. |
| `history.json` | The last 20 operations, with their logs. |
| `backups/` | `<UTC timestamp>-<old sha>.tar.gz`, the newest 3 kept. Read-only on the page in A1. |
