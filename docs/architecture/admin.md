# The OpenFit Admin (A1, A2, A4, A5, A6, U1, S1, S2, R1): updates behind buttons, users, its own update and revert, the app shelf, organisation repos, setup links, the Admin Portal, downloads and restore

_Built: A1, A2, A4, A5 and A6, slices of the Admin in
[distribution.md](distribution.md), U1's setup links (the user login,
[auth.md](auth.md)), S1, the Admin Portal: the page in OpenFit's style,
S2: reverting the Admin, downloads without the password and deleting a
backup, and R1: restoring a backup as a new user. The code is `admin/`;
the tests are `tests/test_admin_*.py`._

Until A1, OpenFit was updated by SSHing to the docker host and running:

```
cd ~/containers/openfit && git checkout main && git pull && docker compose up -d --build
```

A1 put exactly that behind buttons in a small web admin on port 8090, and
added the safety nets the command line never had: a dirty-tree check, a
backup of the data before every update, a health check afterwards, an
automatic undo if the new version doesn't come up, and an Undo button.

A2 lets the Admin add more **users**, each with their own OpenFit: their own
container, their own storage and their own port. Update and Undo move every
user together, so there's one version of OpenFit on the server at a time.

A4 lets the Admin **update itself**: Update the Admin, in place of a command
to run on the server, through a short-lived helper container that swaps the
Admin over and puts the old one back if the new one doesn't answer.

A5 builds the **app shelf**: a Docker volume the Admin fills with the
official apps (the repo's `library/`), which every user's OpenFit mounts
read-only at `/library`. User containers stay fully offline: there's no
network between containers, only the shared read-only volume. Every app on
the shelf is available to every user (no allowlist). An app-only change goes
out with **Update apps**: no rebuild and no restarts. Each user gets the
mount at their next recreate, and **Restart** does that on its own. The user
system's Browse (B1) reads `/library` and installs from it.

S1 is the **Admin Portal**: the page redrawn in OpenFit's own style (its
tokens and components, copied, and held to OpenFit's by a test), as a
header, a left column of four panels that are always there (system
information, versions, repos and the activity log) and two tabs on the
right (Users and Repo), with the few reads behind them: the server's
name, Docker version, CPUs and memory, the free disk, each version's last
update and what it brought, and the shelf's sources and apps, named by
`library/repo.yaml` and each app's `app.yaml`. Every route, operation,
confirm and safety rule is as it was.

A6 adds **organisation repos**: GitHub repos of apps other than OpenFit's
own, each added with a read-only deploy key the Admin makes for it, cloned
over SSH into the Admin's storage and published to the shelf under the
source its `repo.yaml` declares, beside the official apps. FRMS moved to the
first, `openfit-aviation`, and left the official library.

S2 wires up the three places S1 left disabled. **Revert the Admin** takes
it back to the version before its last update, through the same hand-over
as the update, the other way round. **Download** hands over a backup, or a
removed user's kept data, as a `tar.gz` **without the password**: the
user's `auth.json` and `.session_key` are never in a downloaded file.
**Delete** removes a single backup.

R1 puts a backup back to use: **restore it as a new user**, from a file
uploaded to Add user (a download, moving someone between servers), or
straight from one of the server's own backups or a removed user's kept
data. The archive is checked member by member before anything is created,
a clean copy is written without the password (and, unless they're kept,
the device logins), and the new user always starts **locked**: a setup
link gives them a password.

## What the Admin doesn't do yet

- Rename a user, or change a user's port after it's added. Users on
  different versions.
- Update OpenFit and update itself in one go: Update OpenFit pulls the
  Admin's new code, and Update the Admin is a second button.
- Self-updating installed apps (B3); `replaces` moving an app's data (L3,
  built as B2) or host-version labels (L4). Browse, installing an app from
  the shelf, is the user system's (B1).
- Organisation repos anywhere but GitHub, or over anything but SSH: no
  HTTPS, no tokens. Show some users a repo's apps and not others: every app
  on the shelf is every user's.
- Fetch anything from GitHub but the repo itself and the organisation repos
  added: pull request numbers are read from merge commit titles and linked,
  never fetched.
- Scheduled updates: an update runs now, or is found by the weekly check and
  left for a person.
- The proxy and `.local` names. OpenFit and the Admin are reached at
  `IP:port`.
- The backup file format (a zip with a passphrase,
  [distribution.md](distribution.md)). An Admin backup is a plain `tar.gz`
  of a user's `/data` volume, and that's what a download is and what a
  restore takes.
- Restore over an existing user, or merge a backup into one: a restore is
  always a new user, with a new password.
- Revert more than one step: Revert the Admin goes back to the version
  before its last update, and no further.
- A phone layout: the portal is for a desktop browser, and scrolls sideways
  under 1180px.

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
at that **same path** inside the container, so compose resolves the primary
user's paths (its build context, its `.env`) the same inside and out, and
the containers it recreates carry the same compose labels as ones started from
the host.

The Admin's container (`admin/Dockerfile`) is `python:3.12-slim` with git
and `openssh-client` (for organisation repos, A6), the docker CLI
(`docker:27.5.1-cli`) and the compose plugin (`docker/compose-bin:v2.32.4`),
both copied from their pinned images, and GitHub's host keys,
`/app/github_known_hosts`. It runs as root, for the docker socket.

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
  The one exception to JSON is a restore's upload (R1), `POST
  /api/users/restore`, which is multipart: the header alone guards it
  (another site's form can send multipart, but never a header). A body is
  at most 1 MB on every other route (413), an upload's 2 GB: Flask's
  `MAX_CONTENT_LENGTH` is 2 GB and a small margin, for that route's sake,
  and every other request is held to 1 MB.
- **Forgotten password:** delete `auth.json` from the Admin's volume and
  restart the Admin. It prints a new setup code. A new password ends every
  session made under the old one.

## Users

There are two kinds of user, in one model (a `User` record in
`admin_ops.py`: `name, project, service, compose_file, port, primary,
created_at`; `Admin.users()` lists the primary first, then the added users
in the order they were added):

- **The primary user** is the OpenFit that was already running, adopted in
  place exactly as in A1. Its name is `admin.json`'s `user` (`andy`), its
  compose project `openfit`, its compose file the repo's own
  `docker-compose.yml` (with the Admin's override after it, since A5: the
  shelf's mount), its port `admin.json`'s (8080). Its project builds
  the image every user runs, `openfit-tracker:latest`. It can be stopped and
  started, never removed.
- **An added user** is one the Admin made. Each has the compose project
  `openfit-<name>` and a compose file the Admin wrote itself, at
  `/data/users/<name>/docker-compose.yml` in the Admin's storage, from this
  template, with only the image and the port filled in:

  ```yaml
  services:
    tracker:
      image: openfit-tracker:latest
      pull_policy: never
      ports:
        - "<port>:80"
      volumes:
        - tracker-data:/data
        - openfit-library:/library:ro
      restart: unless-stopped
  volumes:
    tracker-data: {}
    openfit-library:
      external: true
  ```

  Compose names its volume `openfit-<name>_tracker-data`. The app shelf is
  mounted read-only from the Admin's volume, `external`, so compose never
  makes or removes it (see [The app shelf](#the-app-shelf-a5)).

**Why not the repo's compose file?** The root `docker-compose.yml` loads the
repo's `.env` into the container, and that file can hold the primary's
Garmin login and `OPENFIT_SECRET_KEY`. So an added user is never created from
it. Their file has no `env_file`, no `environment` and no bind mount: the
image, a port, a volume of their own and the shelf, read-only, nothing else.
A test holds the written file to the template byte for byte.

The added users are kept in `/data/users.json`:

```json
{"users":   [{"name": "sam", "port": 8101, "created_at": "2026-10-27T12:00:00Z"}],
 "removed": [{"name": "kim", "port": 8102, "removed_at": "2026-10-28T09:00:00Z",
              "volume": "openfit-kim_tracker-data"}]}
```

It's checked strictly on every read, like `admin.json`: anything that isn't
exactly right (a key too many or too few, a bad name, port or time, a name
twice, a port taken, a volume that isn't `openfit-<name>_tracker-data`) is an
error naming the file and the key, which stops the Admin starting. Nothing in
it is ever run as it stands.

**A name** is 1 to 20 lowercase letters and digits (`[a-z0-9]{1,20}`), isn't
`admin`, isn't the primary's name, and isn't already an added user's. **A
port** is a whole number from 1024 to 65535, isn't 8090 (the Admin's), and
isn't the primary's or another added user's. Breaking a rule is a 400; a name
or port already taken is a 409. A removed user's port is free again. Add
user's form suggests the lowest port from 8101 up that none of the Admin's
users (the primary included) has; it can be changed.

The primary's name follows the same rule: since A2, `admin.json`'s `user` must
be 1 to 20 lowercase letters and digits and not `admin` (it goes into backup
names and URLs). The default, `andy`, already is.

### Add user

`POST /api/users {name, port}`. The name and port are checked (400/409), then
the operation lock taken (409 if something else is running), then the rules
checked again under it. Then, still in the request, so a refusal changes
nothing:

1. **Built?** `docker image inspect openfit-tracker:latest`. If there's no
   such image: 409, "OpenFit hasn't been built on this server yet."
2. **Nobody's storage?** For a name that isn't in `removed`,
   `docker volume inspect openfit-<name>_tracker-data`. If that volume is
   already on the server, the Admin has no record of it (a lost `users.json`,
   say): it's someone's data, so it's refused (409) rather than adopted.

Then in the background, as an operation with a log (`kind: "add_user"`):

3. **Write** `/data/users/<name>/docker-compose.yml` from the template.
4. **Start** `docker compose -p openfit-<name> -f <file> up -d --no-build`.
5. **Health check** that user's container, as an update does (below).
6. On success, the user is appended to `users.json` (only now) and the log
   ends "Added sam. Their OpenFit is at `http://<host>:<port>`", the host
   the page reached the Admin at.

On any failure from step 4 on: `docker compose -p openfit-<name> -f <file>
down -v`, the folder `/data/users/<name>/` removed, `users.json` untouched,
and the error reported. If compose's output says `port is already allocated`
or `address already in use`, the message is "Port N is already in use on the
server. Choose another." (The Admin never lists other containers to find used
ports; a clash shows up as `up` failing.)

That `down -v` is the one place `-v` is ever used, and it's safe only because
the project's volume was made moments ago, in this operation, by its own
`up`: step 2 refused a name whose volume was already there.

**A removed user added again** gets their kept data back: their project name
is the same, so compose reattaches their volume. The log says "Their kept data
is reattached.", step 2 is skipped, and a failure takes the container down
with a plain `down`, never `-v`. They come back on the port given, as a new
user (a new `created_at`).

### Start, stop and restart

`POST /api/users/<name>/start`, `/stop` and `/restart`, for any user, the
primary included; an unknown name is 404. Each takes the lock and runs in the
request:

- Stop is `docker compose … stop tracker`.
- Start is `docker compose … up -d --no-build tracker`, which recreates the
  container if the image or the compose files have moved on since it was
  last started (a user stopped through an update starts on the new version).
- Restart (A5) is `docker compose … up -d --no-build --force-recreate
  tracker`, then the health check, as an update does: the container made
  anew from the user's compose files on the image `:latest` names. It's how
  a user picks up the shelf's mount without waiting for an update, and it
  starts a stopped user.

A failure is a 502 with the output behind it, a port clash said as above; a
restart that comes up but doesn't answer is a 502 too ("<name>'s OpenFit
restarted, but didn't answer."). Start and Restart first check the shelf's
volume is there, as every recreate does (below).

### Remove, and delete data

**Remove** (`POST /api/users/<name>/remove`; added users only, 400 for the
primary) is `docker compose … down` with no `-v`: the container goes, the
volume stays. The user moves to `removed` with their volume's name, and
`/data/users/<name>/` is deleted. The page asks first: "Their OpenFit is
removed. Their data is kept until you delete it."

**Delete data** (`POST /api/removed/<name>/delete-data {confirm}`) is the only
way a user's data is deleted:

- `confirm` must be their name, typed exactly, or it's a 400 and nothing runs.
  The page's button only works once the typed name matches.
- The name must be in `removed` (404), and no container may be left in project
  `openfit-<name>` (`docker ps -a` with that label: 409).
- Then `docker volume rm <volume>`, the stored name, checked again against
  `openfit-[a-z0-9]{1,20}_tracker-data` and the user's own. On success the
  entry is dropped. Their backups stay until they're pruned.

### Setup links (U1)

Every user's OpenFit is locked until it has a password ([auth.md](auth.md)),
and a password is set - or a forgotten one reset - through a one-time setup
link. **Setup link** on a user's row (usable while their OpenFit is running)
is `POST /api/users/<name>/setup-link`, for any user, the primary included:

- their container, found by its labels as every command finds it, must be
  running, else 409 ("<name>'s OpenFit isn't running. Start it, then make the
  link.");
- then `docker exec <id> python auth_cli.py setup-link`, exactly: the user
  system's own CLI makes the token, stores its hash (replacing any earlier
  link) and prints one JSON line, `{token, expires_at}`;
- the token is held to `[A-Za-z0-9_-]{40,50}` and the expiry to a UTC stamp,
  and anything but one such line is a 502 ("<name>'s OpenFit didn't give
  back a setup link the Admin recognises."); a failed exec is a 502 with its
  error output, never what it printed;
- the reply is `{link, expires_at}`, the link
  `http://<host>:<their port>/setup#<token>`, the host as your browser
  reached the Admin (as the users' addresses are built).

It takes the lock, so it waits for nothing: while another operation runs
it's a 409. Nothing is stored and it's not an operation: there's no entry in
`history.json`, and the log line is only "Made a setup link for <name>." The
page shows the link once, in a dialog - "Send this to <name>. It works once,
for 24 hours. A new link replaces this one." - with a Copy button (on plain
HTTP the clipboard API isn't there, so it copies the selected text), and
empties it as the dialog closes.

### Managed from the Admin

An added user's compose file lives in the Admin's storage, inside its volume,
not on a host path, so `docker compose` on the host can't drive their project
with a file. Start, stop and remove them from the Admin's page. The primary's
project is still the repo's, and commands run from the host still work on it
as before.

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

## Updating OpenFit, for everyone

**Update OpenFit** is offered when the last check shows anything new
(pulling brings everything, so it's one button), unless only the app library
changed: then it's **Update apps** (below), since there's nothing in OpenFit
to update. It asks first: "This stops every user's OpenFit for about a minute
and backs up everyone's data first." One operation runs at a time (an update,
an undo, a check, or a user action), and a second request gets 409.

The refusals run in the request, so each is an immediate 409 with nothing
changed: no update on offer (with only apps changed: "Only the app library
changed, so there's nothing in OpenFit to update: use Update apps."); a dirty
tree ("The server's copy of OpenFit has local changes: `<files>`. Nothing was
changed."); a repo not on `main`; the shelf's volume not there ("The Admin
needs updating first: its app library storage isn't there yet."). The rest
runs in a background thread, in this order:

1. **Remember.** `HEAD` is noted as `old`. The primary's container is found
   by its compose labels, and its image is tagged `openfit-tracker:prev`.
2. **Pull and build.** `git merge --ff-only origin/main` (if it can't
   fast-forward, it stops: nothing changed), then `docker compose -p openfit …
   build tracker`. **One build, for everyone**: an added user's compose file
   names the image the primary's project builds. Everyone is still running
   the old version. If the build fails, `git reset --hard <old>`, report, and
   stop; nobody was stopped.
3. **Who's running.** For each user, primary first, then the added users in
   order, their container is found and whether it's running recorded
   (`was_running`). A user with no container is skipped, and the log says so.
   Then each running user is stopped (`compose … stop tracker`).
4. **Back up.** Each user's volume is read from their stopped container
   (`docker inspect`, never hard-coded), and a helper container archives it:
   `docker run --rm --label openfit.admin.helper=true -v <volume>:/v:ro -v
   openfit-admin_admin-data:/b openfit-admin:local tar czf
   /b/backups/<stamp>-<old sha>-<name>.tar.gz -C /v .` All of one update's
   backups share one `<stamp>`: they're one **backup set**.
5. **Recreate** each user on the new image, as they were: one that was
   running with `up -d --no-build --force-recreate tracker`; one that was
   stopped with `up --no-start --no-build --force-recreate tracker`, so it's
   created on the new image and stays stopped until it's next started.
6. **Health check** each user that was running. Their container is found
   again (compose made a new one), and `docker exec <container> python -c
   "<urlopen http://127.0.0.1:80/, exit 0 on a 200>"` is polled every 2 s for
   up to 60 s. If anyone doesn't, the undo sequence (below) runs for
   everyone: "Updated, didn't come up, rolled back."
7. **Publish the apps** (A5) from the new `HEAD` to the shelf. A publish
   that fails doesn't undo the update: the shelf stays as it was, the log
   says so ("The update itself is done. Use Publish again, under Repos,
   …"), and the portal's Repos panel shows "The app library couldn't be
   updated" with **Publish again** on OpenFit's row. Then backups are pruned to
   the newest 3 sets, the check is refreshed, "ok".

**Backing out.** If anyone doesn't stop, or any backup fails, the update is
called off: the backups this update took are deleted (a partial set), the code
goes back (`git reset --hard <old>`), `openfit-tracker:prev` is retagged as
`:latest` (the build had replaced it), and every user who was running is
started again on the old version with `up -d --no-build --force-recreate
tracker`. A user who was stopped is left stopped.

The update's record gains `users: [{name, created_at, was_running, backup}]`,
primary first (`created_at` null for the primary).

## Undo, for everyone

The **undo sequence** runs automatically when a user doesn't come up after an
update, and is also the **Undo last update** button. Undo is offered only
after a successful update, for that update only: the latest update or undo is
that update, the repo is still on the version it updated to, **every backup
in its set is still here**, and **its users are exactly today's** (nobody
added or removed since; someone removed and added again is someone new, told
by their `created_at`). Otherwise there's nothing to undo. It warns:
"Anything recorded since the update is lost, for every user."

1. Stop every recorded user.
2. Restore each one's own backup into their volume, with a helper:
   `sh -c "find /v -mindepth 1 -delete && tar xzf /b/backups/<file> -C /v"`.
   The file name is from the Admin's own listing, checked against the backup
   name pattern. Restoring matters because the new version may have migrated
   the database.
3. `docker tag openfit-tracker:prev openfit-tracker:latest`.
4. `git reset --hard <old>` (as the owner).
5. Recreate each user as they were (running, or created and stopped, as in
   step 5 above), then health-check the ones that were running.
6. Publish the apps again from `HEAD` (A5), so the shelf matches the code
   again: after an Undo, the old version's apps. A rollback does the same,
   though an update's publish only runs once everyone answered, so the shelf
   is usually there already and nothing is rewritten.

**If a restore fails** for one user, it stops there: nobody is started, and
the code and image stay on the new version (starting anyone on a
half-restored set would be worse). The log says exactly who is where: "andy:
data put back, stopped; sam: data not put back (the backup is …), stopped;
kim: data not put back yet, stopped." If a user doesn't stop in step 1,
nobody's data is touched, and the log says the same.

A user the update skipped (no container) has no backup: the automatic
rollback leaves them as they are, and the Undo button isn't offered for that
update.

An update recorded by A1 (one `backup`, no `users`) is read as the primary
alone, running, with that backup, so it can still be undone.

**Progress.** Each operation has a log of `{t, text}` lines in plain words
("Backing up sam's data…"), with the tail of a command's output when it
fails. `GET /api/op` returns the running operation, else the last one; the
page polls it every second while one runs. The last 20 operations are kept
in `/data/history.json`; one the Admin was stopped in the middle of reads
"interrupted" after a restart.

## Backups

`<stamp>-<old sha>-<name>.tar.gz` in `/data/backups/`, one per user per
update, matched by
`(\d{8}T\d{6}Z)-([0-9a-f]{4,40})(?:-([a-z0-9]{1,20}))?\.tar\.gz`. An A1
backup, with no name in it, is the primary's. `list_backups()` gives each its
`user`; the page shows it. Backups are pruned **by set**, meaning by stamp:
after a successful update the newest 3 sets are kept and every file of an
older set is deleted. A removed user's backups stay until their set is
pruned. They hold everything in the user's `/data`, the password included,
because Undo puts them back as they were: an Undo never locks anyone out.

### Deleting one (S2)

`POST /api/backups/<name>/delete` (the `X-OpenFit-Admin` header and a JSON
body, as every write): `<name>` must match the pattern above and be in the
Admin's own listing, else 404; under the lock, so 409 while anything runs;
then that one file is unlinked, and the Admin's log says "deleted the backup
`<name>`". In `GET /api/status`, each backup's `undo_relies` is true when
the Undo on offer would put it back (its update's set): deleting one of those
takes Undo away, and the popup's confirm says so, "Undo for that update will
no longer be offered."

## Downloads, without the password (S2)

**A downloaded file never holds the user's OpenFit password** (`auth.json`,
its hash) **or its sessions' key** (`.session_key`). Those two files, at the
root of the user's `/data` - tar's `./auth.json` and `./.session_key`, and
the same names without the `./` - are left out of every download. Nothing
else is: the database (and its `-wal`/`-shm`), the apps, the device logins
and the credentials key (`.secret_key`) are all there, as the warning before
the download says: "This file holds everything in that OpenFit except its
password: its data, apps and device logins. Keep it somewhere safe. Whoever
restores it sets a new password with a setup link." The backups on the
server are unchanged and still hold everything (above).

Both downloads are GETs that change nothing, so the page uses a plain link
(`download`), and they need a session but not the `X-OpenFit-Admin` header;
the session cookie is `SameSite=Strict`, so another site can't send it. Both
are refused, 409, while an operation runs, so a download never races an
update's backup or its prune. The check and the start (the file opened, or
the helper started, and the first chunk read) happen under the lock; then
the file streams without it, so a download in progress doesn't hold up the
Admin. A failure before the first byte is a 502 with the reason; one part-way
cuts the download off (the browser shows it failed, never a short file as
whole). The Admin's log gets one line, "Downloaded `<file>` (no password)",
and never anything that's in it.

- **A backup**: `GET /api/backups/<name>/download`, `<name>` as Delete takes
  it (404 otherwise). The backup is read with `tarfile` and streamed as a new
  `tar.gz`, every member as it was - header, mode, times, owner, bytes - but
  the two left out. A thread writes the new archive into a pipe the response
  reads from, a chunk at a time: never a temp file, and a chunk of memory
  whatever the backup's size. Named
  `<backup without .tar.gz>-nopassword.tar.gz`, as an attachment.
- **A removed user's kept data**: `GET /api/removed/<name>/download`,
  `<name>` in `users.json`'s removed list (404 otherwise), with no container
  left in their project (409, as Delete data checks). Streamed from a helper,
  exactly:

  ```
  docker run --rm --label openfit.admin.helper=true --log-driver none
    -v openfit-<name>_tracker-data:/v:ro openfit-admin:local
    tar czf - -C /v --exclude=./auth.json --exclude=./.session_key .
  ```

  through `Runner.stream()`, which yields the command's output as it comes,
  kills it if the browser goes away, and raises on a non-zero exit with its
  error output. `--log-driver none` because Docker would otherwise keep a
  copy of the whole archive in the helper's container log while it runs.
  Named `openfit-<name>-kept-<UTC date>-nopassword.tar.gz`. If the browser
  goes away, the `docker run` client is killed; the helper itself, reading a
  volume nothing else uses, finishes on its own and is removed (`--rm`).

## Restoring a backup as a new user (R1)

A backup can be put back to use as a **new user**: never over an existing
one, and never merged into one. The source is one of three:

- **A file**, uploaded: a download from this Admin or another (moving
  someone between servers is a download on the old one and a restore on
  the new). Add user's third control, **Restore from a file**: with a file
  chosen, Add user becomes **Restore user**, and **Keep device logins**
  appears beside it, ticked.
- **One of the server's own backups**, or **a removed user's kept data**,
  with no download and upload: **Restore as a new user** in its popup under
  User data opens a small form - Name, Port (the suggestion prefilled) and
  Keep device logins, ticked - and **Restore**.

Both are one operation, `kind: "restore_user"`, under the lock, with a log
as Add user's has.

### The routes

- `POST /api/users/restore`: multipart - `name`, `port`, `keep_devices`
  (`true` or `false`) and `file` - with the `X-OpenFit-Admin` header and a
  session. The file is written to `/data/restore-uploads/<random>.tar.gz`
  (0600, in a folder 0700) as it arrives, through Werkzeug's form parser
  with the file as its stream: never held in memory, and stopped with a
  413, "That file is too large: a backup to restore is at most 2 GB.", the
  moment it passes 2 GB (a body whose `Content-Length` says more is refused
  before it's read). A form that isn't whole (no file, two files, a field
  missing) is a 400. Refused, cut off or too large, the file is deleted
  there and then; once the restore has started, the restore deletes it when
  it ends, whatever the outcome.
- `POST /api/users/restore-from {name, port, keep_devices, backup |
  removed}`: exactly one of `backup` (a name `BACKUP_NAME` matches and the
  Admin's own listing holds, else 404) or `removed` (a name in `removed`,
  else 404, with no container left in their project, else 409).

Before the operation starts, in the request, as Add user's: the name and
port by Add user's rules (400, or 409 for one taken), and **a name with
kept data is refused**, 409, "<name> has kept data on this server. Choose
another name." (to bring a removed user back as they were, add a user of
that name instead: their kept data is reattached); `keep_devices` a
boolean (400); the lock (409); OpenFit's image built (409); the app
shelf's volume there (409); and no volume under the new name the Admin
has no record of (409), so the failure path's `down -v` can only ever
take what this restore made.

### The archive's checks, before anything is created

A removed user's kept data is first packed into a file in
`/data/restore-uploads/` by the download helper's own form, its output
written to the file instead of a browser, with **no exclusions** (the
clean archive below leaves the password out, and more):

```
docker run --rm --label openfit.admin.helper=true --log-driver none
  -v openfit-<name>_tracker-data:/v:ro openfit-admin:local tar czf - -C /v .
```

Then the archive (the upload, the backup itself, or the packed file) is
opened with `tarfile` (`r:gz`) and **every member walked before anything
is written**:

- only regular files and folders: a symbolic link, a hard link, a device
  or a FIFO refuses the whole archive;
- each name normalised, its leading `./` stripped, and refused if it's
  absolute, has a `..` part, a NUL byte, an empty or `.` part, or a part
  over 255 characters; and no name twice (a second `tracker.db` would be
  the one extracted, unchecked);
- at most 200,000 members and 8 GB unpacked, the sum of the members'
  sizes, read from their headers: an archive bomb stops there;
- `tracker.db` there, copied out (with its `tracker.db-wal`, if it has one:
  a database not closed cleanly keeps recent writes there, its newest
  migration among them) into a folder of the Admin's own and opened
  read-only with `sqlite3` (`file:…?mode=ro`, defensive, untrusted schema
  off): `PRAGMA integrity_check` must say `ok`, and a `schema_migrations`
  table must be there;
- **its version**: `MAX(version)` of `schema_migrations` must be at most
  the newest migration of the OpenFit this server runs - the highest `NNN`
  of `app/migrations/NNN_*.sql` in the repo at `HEAD` (`git ls-tree`), by
  the user system's own file name rule - else "This backup is from a newer
  OpenFit than this server runs. Update OpenFit first." An older one is
  fine: the user system migrates it as it starts.

Each refusal is a failed operation with a plain message ("… Nothing was
created."): no compose file, no container, no volume, no helper run, and
the restore's files deleted.

### The clean archive

The Admin then writes a **clean archive** of its own - a fresh `w:gz` of
the checked members, read from the original, never the original itself -
into `/data/restore-uploads/<random>-clean.tar.gz` (0600). Each member is
a new header, `./<name>`, a regular file or a folder with its mode (no
setuid, setgid or sticky bit), owner, group and time; nothing else of the
original's header is carried, and each file's bytes are as they were.
Always left out:

- `auth.json` and `.session_key`, so the user starts **locked**: a setup
  link sets their password, as for a new user;
- `app_updates.json`, so their library apps' update notes start afresh.

With **Keep device logins** unticked, also left out:

- `.secret_key`, the key their device credentials are stored under: their
  OpenFit makes a new one, can't read the stored credentials, and says to
  remove and re-add each device;
- everything under `.garminconnect/`, Garmin's own login tokens.

Then the user removes and re-adds each device in their OpenFit. The log
names what was left out.

### Creating the user

Then, in order:

1. **Their compose file**, from the template, as Add user writes it.
2. **Created, not started**: `docker compose -p openfit-<name> -f <file> up
   --no-start --no-build` makes the container and its volume, brand new and
   empty.
3. **Filled**, by a helper, exactly:

   ```
   docker run --rm --label openfit.admin.helper=true --log-driver none
     -v openfit-<name>_tracker-data:/v -v openfit-admin_admin-data:/b:ro
     openfit-admin:local tar xzf /b/restore-uploads/<random>-clean.tar.gz -C /v
   ```

   The volume was made a moment ago, so nothing is overwritten. The helper
   only ever extracts a name the clean archive's pattern matches
   (`restore_helper_args()`), never an upload or a backup as it came: **no
   archive is ever extracted by the Admin process itself**, and only the
   clean one by the helper.
4. **Started**: `up -d --no-build`, then the health check, as for Add user.
   Its migrations run as it starts.
5. On success, the user is appended to `users.json` (only now), and the
   log ends "Restored <name>. They start locked: use Setup link to give
   them a password." The operation's result in the Activity log offers
   **Setup link** straight away.

A failure from step 2 on is taken back with `down -v` and their folder
removed, `users.json` untouched - Add user's take-back, the second place
`-v` is used, and safe for the same reason: the project and its volume
were made moments ago, in this operation, by its own `up --no-start`, and
the request refused a name whose volume was already there. What they held
is a copy of a backup that is still where it came from. A port clash says
so, as for Add user.

The operation records `user: {name, port}`, `source` (`{kind: "upload"}`,
`{kind: "backup", name, user}` or `{kind: "removed", name, user}`) and
`keep_devices`. Everything in `/data/restore-uploads/` - the upload, the
packed kept data, the database's copy and the clean archive - is deleted
when the restore ends, whatever the outcome, and anything there at the
Admin's startup is a leftover, deleted then.

### Device logins: the warning

With Keep device logins ticked, restoring the server's own backup or kept
data of someone who is **still a user here** warns, in the form: "<name> is
still a user here. Two OpenFits syncing the same Garmin or Google account
can sign one of them out. Untick Keep device logins, or remove <name>
first." The backup's user is read from its name. An uploaded file carries
no user name, so there's no warning for one.

## The page: the Admin Portal (S1)

`admin/templates/index.html`: one self-contained page, no external requests
and no web fonts, for a desktop browser (a `min-width` of 1180px, scrolling
sideways below it). Every name, log line and summary is set as text, never
markup.

**OpenFit's look, copied.** The theme tokens are OpenFit's own, copied from
`app/templates/index.html`'s three token blocks: the light `:root`, the dark
one under `@media (prefers-color-scheme:dark)` for
`:root:not([data-theme="light"])`, and `:root[data-theme="dark"]`. Only the
tokens the Admin uses are there, each with OpenFit's value in every block
OpenFit gives it one, and the font stacks are OpenFit's. A drift test
(`tests/test_admin_portal.py`) parses both pages' blocks and fails when any
Admin token's value differs from OpenFit's in any block: when OpenFit's
colours change, recopy them. The components are OpenFit's rules, copied: the
brand, the date pill and the mono chips under it, the skewed tabs, cards with
a title and a round button after it (OpenFit's voice button, its triangle
here the panel's fold), the buttons, the pills, Your data's expandable rows,
the modal, the label and value list and the danger panel.

**The header** is OpenFit's: the brand mark and `OpenFit`, then ADMIN
PORTAL in the display font, in letter-spaced caps; on the right the date
pill and under it two chips. **settings** opens a small popup holding **Log
out**, the only setting for now. **theme** steps auto, light, dark, as
OpenFit's does: the same states and the same `data-theme` attribute, stored
in this browser as `openfit-admin-theme`.

**Two columns** under it: on the left, about 40%, four panels that are always
there; on the right, about 60%, a tab strip with **Users** (the default) and
**Repo**, the choice stored in this browser as `openfit-admin-tab`. Those two
keys are all the page stores. Each panel folds with the button after its
title, for the page's life. Lists sit in scroll boxes whose rows have a
fixed height, each box showing exactly that many rows before it scrolls (a
shorter list, a shorter box).

The left column:

- **System information**: Server (Docker's name for the host), Docker (its
  version), CPU and memory, Disk free (`os.statvfs` of the Admin's `/data`,
  where the backups are kept), Backups (their total size, and how many),
  Users ("2 running · 1 stopped", and how many aren't found, if any) and
  Admin up since (when this Admin process started). The first three come
  from `docker info --format '{{json .}}'`, run at most once a minute (its
  answer, or a failure, is kept for the minute): of all it says, the Admin
  keeps only `Name`, `ServerVersion`, `NCPU` and `MemTotal`
  (`server_fields()`: printable text up to a length, and whole numbers, else
  null). It deliberately leaves the rest out, never showing or storing it:
  how many containers and images the host has, their names, the host's id,
  network, plugins, labels and warnings. The Admin keeps to its own users,
  and doesn't report on what else the server runs.
- **Versions**: a row for the **Admin** and one for the **User** (every
  user's OpenFit), each its version, when it was last updated and **Update
  available** when there's one. The User's version is `HEAD`'s short sha and
  date. The Admin's is the short sha of the newest commit touching `admin/`
  at or before `HEAD` (`git log -1 -- admin/`) - unless the running Admin's
  code isn't the repo's `admin/` (the fingerprint the Admin update already
  compares), when it's shown as **older**, and its update is on offer. Last
  updated is the `finished_at` of the newest successful operation of its
  kind (`update`, `admin_update`), else "—". Update available is an accent
  pill, shown for the User when Update OpenFit is offered and for the Admin
  when `admin_update()` says one is available; it opens the update popup.
  A row's name opens its **last update** popup. Under the rows: when the
  last check ran (and whether GitHub has new commits), **Check now**, and a
  failed check's error.
- **Repos**: a row for each source on the shelf and each organisation repo
  (A6), OpenFit first, then the rest by name, three rows high: its name
  (from `repo.yaml`; `owner/repo` for a repo not published yet), its
  version (the published commit, short) and when it was last published.
  OpenFit's row also offers **Update available** when Update apps is on
  offer (the update popup for the apps, its Proceed the existing Update
  apps), **Comes with the OpenFit update** when app changes are waiting but
  OpenFit changed too (the User's update popup instead), and **Publish
  again** when the shelf is behind the code, with why under the box. A
  repo's row offers **Update available** when it's behind (its popup's
  Proceed `repo_update`), and says "Couldn't reach GitHub" when its last
  fetch failed, and its state when it isn't connected and published. The
  name opens its **last publish**: when, the commit, and the apps
  published (and, for a repo, where it's from).
- **Activity log**: the running operation's log, live, at the top (polled
  every second, as before), then every operation the history keeps (20),
  newest first, each a row - what, when, an outcome pill - that opens to its
  summary and log, in a box about eight rows high. An operation watched to
  its end opens by itself. A restore that worked (R1) offers **Setup link**
  for the restored user in its result, while they're a user and running.

The **Users** tab:

- **Users**: alphabetical by name, ten rows high, each its name and state
  (Running, Stopped, Not found); open, its address
  (`http://<host>:<port>`, a link), its state with "Restart to see the app
  library" beside it for a container without the shelf's mount, and **Stop**
  or **Start**, **Restart**, **Setup link** and **Remove** (with its
  confirm). The primary has no Remove; in its place: "This is the server's
  main OpenFit: new versions are built from it, so it can't be removed."
- **Add user**: one row - Name, Port (the suggestion prefilled, editable),
  **Restore from a file** (R1) and Add user - with the hint under it. With a
  file chosen, Add user is **Restore user**, and under the row **Keep device
  logins** (ticked) and **Clear file**; the hint says what a restore does.
  A file over 2 GB is refused on the page, before anything is sent; while
  one uploads, the row says "Uploading <file>… keep this page open."
- **User data**: the backups and the removed users' kept data, newest first,
  ten rows high. A backup is its user, date and size; kept data a red warning
  triangle, the user and "Removed <date>". A row opens a popup: a backup's
  details - its user, the update it belongs to (read from the history, else
  the old version in its name), date, size and file - with **Download** and
  **Delete**; kept data's **Delete data**, behind typing their name, with
  **Download** beside it. Download opens the warning (above) and the link
  itself, "Download without the password"; Delete a confirm, "Delete this
  backup? This deletes `<name>` from the server, for good.", with the Undo
  line when `undo_relies`. Both popups also have **Restore as a new user**
  (R1): its small form - Name, Port (the suggestion prefilled), Keep device
  logins (ticked), the device-login warning when it applies, and Restore;
  kept data's adds that to bring them back as they were is to add a user of
  that name instead. All of these are off while an operation runs.
- **User version info**: what every user is running - the version (short
  sha and date), `HEAD`'s subject, and the pull requests the last update
  that worked brought, as links. Read only.

The **Repo** tab: **Add repository** (A6: the repo's address, then its
deploy key, Copy key and the three steps, then Connect), then the sources,
OpenFit first and the rest by name, each a row (name, and its version - or,
for a repo not connected and published, its state) that opens to its
description in a small box, an Applications heading, and each app as a row
of its own that opens to its description, or "No description." An
organisation repo's row opens first to its GitHub address (a link), state
and why, source and last check, then **Connect** while it's pending - with
**Show key**, its public key again - and **Remove**, behind its confirm.

**The popups** are OpenFit's modal; Escape, a click outside or Close shuts
one.

- **The update popup**, shared by the User, the Admin and the app library:
  what will happen, in a sentence (the confirm each had: "This stops every
  user's OpenFit for about a minute and backs up everyone's data first.",
  the Admin's restart, "Publishes the new apps to every user. Nobody's
  OpenFit restarts."), the changes grouped as a check reports them, with
  commit subjects and pull request links (for the Admin, the changes its
  update brings: those pulled since the code it was built from, and any
  still on GitHub), then **Proceed** and **Cancel**. Proceed starts the
  existing operation (`POST /api/update`, `/api/admin/update` or
  `/api/library/update`), and the activity log shows it live. While the
  Admin's new code isn't pulled yet it says "Update OpenFit first", with
  Proceed off; otherwise it also shows the same update by hand.
- **The last update popup**: when, the outcome, the versions it went
  between, when it was undone if it was, and the commits it brought - `git
  log --no-merges --format=%h%x09%s <old>..<new>` over the operation's
  recorded commits (the Admin's limited to `admin/`), the pull requests read
  from the merges in the range, as a check reads them. The User's offers
  **Undo** while Undo is on offer, with its warning; the Admin's,
  **Revert the Admin** (below), with its confirm when it's on offer, and
  when it isn't, the button off and the one line saying why. The Admin's
  says when it was reverted, as the User's says when it was undone.
- **The setup link**, as before: shown once, kept nowhere, the field
  emptied as it closes.

An Admin update now records its range as an update does: `new`, the `HEAD`
it builds, and `old`, the code the running Admin was built from
(`_admin_base()`: the `new` of the last successful Admin update, else the
`old` of the first successful OpenFit update after it, the one that pulled
the Admin's new code). An Admin update an older Admin started (the S1
rollout's own) is given them when the new Admin finishes it.

`GET /api/status` keeps every field it had and adds:

- `system`: `{server: {Name, ServerVersion, NCPU, MemTotal} | null,
  disk_free, backups_bytes, users: {running, stopped, missing},
  admin_up_since}`.
- `versions`: `{user: {commit, short, date, subject, last_updated,
  update_available, last, current}, admin: {commit, short, date, subject,
  older, last_updated, update_available, can_self_update, command, pending:
  {commits, prs}, last}}`; `last` the newest operation of the kind and
  `current` the newest successful update, each `{id, kind, outcome, summary,
  started_at, finished_at, old, new, changes: {commits, prs} | null,
  undone_at}`. A range between two commits never changes, so it's read once.
- `repos`: `[{source, name, description, official, commit, short,
  published_at, apps: [{id, name, description}], current, error, update}]`,
  from the shelf's `index.json`; `current`, `error` and `update` (`"apps"`,
  `"openfit"` or null) OpenFit's alone.
- `activity`: every operation in the history, newest first, each `{id,
  kind, outcome, summary, started_at, finished_at, log, user}`.
- each of `backups` gains `update`: `{id, old, new, started_at, outcome}` of
  the update that took it, or null once that's gone from the history.

S2 adds:

- `admin_revert`: `{offered, reason, update, old}` - whether Revert the Admin
  is on offer, and the one line saying why not; the Admin update it would
  reverse (its id) and the version it goes back to (short), where known.
- each of `backups` gains `undo_relies` (above).
- a `last` Admin update's `undone_at` is when a revert of it worked.

Every state-changing call still goes through the page's one `fetch` wrapper
(the `X-OpenFit-Admin` header) and needs a session.

## Updating the Admin itself (A4)

An Admin can't replace itself: stopping its own container kills the process
doing the stopping. So it builds its new image while it keeps running, then
hands the swap to a short-lived **helper** container, which stops the old
Admin, starts the new one, checks it answers, and puts the old one back, code
and settings, if it doesn't. Then the helper exits.

**The helper runs the old Admin's image**, the version that's running now and
known to work: the running container's image, tagged `openfit-admin:prev`
just before the build. A broken new version can only fail the health check;
it never gets to run the swap or the rollback.

### When it's offered

There's one repo, so there's one pull: Update OpenFit pulls the Admin's new
files along with everything else. The Admin tells its files have moved on by
comparing the files it's running (its image holds all of `admin/`) with the
repo's `admin/`, dotfiles and caches left out. `GET /api/status`'s
`admin_update` is `{available, pulled, commits, command, can_self_update}`:

- **Pulled**, and nothing more for the Admin waiting on GitHub
  (`can_self_update`): the Admin's row under Versions shows **Update
  available** (its version "older"), and its update popup's **Proceed** runs
  it. The command to run by hand stays in the popup, smaller, as a fallback:

  ```
  cd ~/containers/openfit && docker compose -f admin/docker-compose.yml -p openfit-admin up -d --build
  ```

- **Admin commits on GitHub not pulled yet**: the popup lists them and says
  "Update OpenFit first: it brings the Admin's new code with it.", with
  Proceed off.
  This holds even when some Admin changes are already pulled: Update OpenFit,
  then one Admin update.

`POST /api/admin/update` starts it, a background operation (`kind:
"admin_update"`) behind the same lock as every other: 409 while anything else
runs, and 409 when there's nothing to apply ("Update OpenFit first: …", or
"The Admin is up to date.").

### What the running Admin does

1. **Find itself**: its container by the labels
   `com.docker.compose.project=openfit-admin` and
   `com.docker.compose.service=admin`. It must be exactly one, else the
   operation fails with nothing changed. Its image id is read
   (`docker inspect`) and checked.
2. **No helper already there**: a container named `openfit-admin-swap`
   means "An Admin update is already under way. Nothing was changed."
3. **Keep the current image**: `docker tag <image id> openfit-admin:prev`.
4. **Snapshot its settings**: `admin.json`, `users.json`, `auth.json`,
   `secret_key`, `history.json` and `last_check.json`, each if present, copied
   into `/data/self-update/<stamp>/` (0700; the 0600 files stay 0600). The
   newest 3 snapshots are kept. They're small, and backups aren't copied.
   This is what lets a rollback undo anything a new version might change in
   the Admin's own files.
5. **Build the new Admin**: `docker compose -p openfit-admin -f
   <OPENFIT_REPO>/admin/docker-compose.yml build admin`. The old Admin keeps
   running. If the build fails: reported, and nothing changed (the `:prev`
   tag is harmless).
6. **Start the helper**, detached, with exactly:

   ```
   docker run -d --rm --name openfit-admin-swap --label openfit.admin.helper=true
     -v /var/run/docker.sock:/var/run/docker.sock
     -v <OPENFIT_REPO>:<OPENFIT_REPO>
     -v openfit-admin_admin-data:/data
     -e OPENFIT_REPO=<OPENFIT_REPO>
     openfit-admin:prev
     python /app/admin_swap.py <stamp> <op id>
   ```

   The log says "Handing over to the helper. The Admin restarts in a moment,
   and this page reconnects by itself." The operation's log so far is saved,
   and copied into the snapshot's `history.json` too, so a rollback puts it
   back with the rest. The operation stays `running`, and the lock stays
   held: the helper finishes it (below).

Every compose command on the Admin's own project goes through one function,
`admin_compose()` (`admin_ops.py`), which runs only `build admin`, `stop
admin` and `up -d --no-build --force-recreate admin` and refuses anything
else, and it's called from exactly three places: step 5 (the build), and the
helper's `stop()` and `recreate()`. The Admin's own steps never stop it:
only the helper does, and every stop is followed by an `up`.

### What the helper does

`admin/admin_swap.py`, a small module on `admin_ops`' `Runner`, patterns and
health code. Its logic is `swap(runner, repo, data_dir, stamp, op_id, clock,
sleep)`, which the tests call directly. In order:

1. **Wait 3 seconds**, so the Admin's reply to the button reaches the browser.
2. **Swap in the new Admin**: `docker compose -p openfit-admin -f
   <repo>/admin/docker-compose.yml up -d --no-build --force-recreate admin`.
   That stops and replaces the Admin. The helper isn't part of that project,
   so it carries on.
3. **Health check**: the Admin's container found by its labels, then `docker
   exec <id> python -c "<urlopen http://127.0.0.1:8090/, exit 0 on a 200>"`,
   the user system's check on port 8090, every 2 s for up to 60 s.
4. **Healthy**: the result is written as `ok`, and the helper exits.
5. **Not healthy** (or the new Admin didn't start), it rolls back:
   `docker tag openfit-admin:prev openfit-admin:local`; **the Admin stopped**
   (`docker compose -p openfit-admin -f <repo>/admin/docker-compose.yml stop
   admin`, T1), so a slow new Admin, still running, can't write over the
   settings as they go back; the snapshot restored, each file in
   `/data/self-update/<stamp>/` copied back over `/data/`, and any of the six
   names the snapshot doesn't have removed; `up -d --no-build
   --force-recreate admin` again, which starts it, and the health check. A
   stop that fails is logged and the settings are put back all the same (the
   recreate replaces the container either way, and an Admin on the wrong
   settings is the worse of the two). The result is
   `rolled_back` if the old Admin answers, else `failed`, with the log saying
   exactly where things stand (which image `:local` is, whether the settings
   are back) and the commands to run by hand.

Its log goes to `/data/self_update_result.json` as it runs, `{op_id, stamp,
outcome, lines: [{t, text}]}`, rewritten after each step (`outcome`
`running` until the end). So even a helper that dies part-way leaves a trail.
It never touches the `openfit` project or any `openfit-<name>` project, and
never uses `down`, `-v`, `rm` or a volume. It refuses to run (a `failed`
result, nothing changed) if it isn't given a snapshot it can find.

### Finishing the operation

When an Admin starts, before it marks interrupted operations, it looks for
`/data/self_update_result.json`:

- **A finished result** (`ok`, `rolled_back` or `failed`): the operation with
  that `op_id` gets the helper's lines appended to its log and its outcome,
  with a summary, and the file is deleted. It's never marked interrupted.
  `ok` reads "The Admin updated itself to `<short sha>`.", the repo's `HEAD`;
  `rolled_back` "The new Admin didn't come up, so the old one was put back,
  with its settings as they were."; `failed` "The Admin update didn't finish.
  The log says where things stand, and what to run on the server."
- **Still `running`**: the helper is at work, health-checking this very Admin.
  The operation stays running, the lock stays held (so nothing starts that a
  rollback would cut off), and it's finished as above once the helper has
  gone: the next time the operation is read or the lock asked for, the Admin
  looks for the helper's container by name (`docker ps -a --filter
  name=^openfit-admin-swap$`), and once it's gone reads the result. The page
  polls the operation while it runs, so that's within a second or two.
- **No result file**, and an Admin update still `running` (the helper never
  ran): it's marked interrupted, as any operation is.

The old Admin does the same after handing over: if the helper goes without
swapping it out (it died before step 2), the old Admin, still running,
finishes the operation as failed, "The helper stopped part-way.", and frees
the lock.

### The page

Update the Admin asks first, in its update popup: "The Admin restarts on its
new version. This page will be unavailable for up to a minute and reconnects
by itself. If the new version doesn't start, the old one is put back." Then
the activity log shows the operation live. When its requests start failing,
the page shows "The Admin is restarting…" and polls `GET /` every 2 seconds;
once that answers, it reloads, and the finished operation appears in the
activity log. The login session
survives, because `secret_key` is unchanged; if a rollback put back an older
`auth.json`, the page simply shows the login.

### Installing A4: the manual command one last time

The Admin running before A4 has no `admin_swap.py` in its image, and the
helper always runs the current image. So A4 itself is installed the old way,
once: **Update OpenFit**, then run the command its note showed on the server.
From then on, Admin updates are the button.

### If the helper dies part-way

Activity shows the operation's log as far as the helper got, ending "The
helper stopped part-way." The Admin may then be on either version: whichever
is answering finished the operation. To get back to a known state, on the
server:

- the version in the repo: `cd ~/containers/openfit && docker compose -f
  admin/docker-compose.yml -p openfit-admin up -d --build`;
- or the old one: `cd ~/containers/openfit && docker tag openfit-admin:prev
  openfit-admin:local && docker compose -f admin/docker-compose.yml -p
  openfit-admin up -d --no-build --force-recreate admin`.

The settings the update started with are in the Admin's volume, under
`self-update/<stamp>/`, if they need putting back by hand. If no Admin
answers at all, `docker logs openfit-admin-admin-1` says why.

## Reverting the Admin (S2)

**Revert the Admin** takes the Admin back to the version before its last
update, and its settings as they were at that update. OpenFit and the users
aren't touched. It's the update's hand-over the other way round, and the
helper runs **the image the Admin runs now** (`openfit-admin:local`), the
one known to work and the one that holds the revert, never the one it goes
back to.

### When it's offered

All of these, else the popup's button is off and one line says why:

- the newest Admin update worked (`ok`);
- its settings snapshot, `/data/self-update/<stamp>/`, is still there (an
  Admin update records its `snapshot` stamp; one an older Admin started is
  given it from the helper's result when it's finished);
- the users haven't changed since: the snapshot's `users.json` and the live
  one hold the same people - each added user by name and `created_at`, each
  removed one by name and `removed_at` - so putting `users.json` back loses
  nobody, and brings back nobody deleted ("Users were added or removed since
  that update, so it can't be reverted.");
- nothing is running;
- `openfit-admin:prev` is still on the server (`docker image inspect`);
- the Admin isn't running `:prev` already (its container's image id isn't
  `:prev`'s): it hasn't been reverted.

The Docker checks run last, only when the rest hold. The confirm: "The Admin
goes back to `<old short>` and its settings as they were at that update.
OpenFit and your users aren't touched. The page reconnects by itself."

### What the running Admin does

`POST /api/admin/revert` starts it, a background operation (`kind:
"admin_revert"`, its `undoes` the update's id, `old` the version it leaves
and `new` the one it goes back to), behind the lock; 409 with the reason when
it isn't offered.

1. **Find itself**, as an update does, and read its image id.
2. **No helper already there** (`openfit-admin-swap`): else "An Admin update
   or revert is already under way. Nothing was changed."
3. **Keep the current image**: `docker tag <image id> openfit-admin:next`.
4. **A fresh settings snapshot**: the six files as they are now, under a new
   stamp, for the fallback. Pruning (the newest 3) never takes the update's
   snapshot, which the helper puts back.
5. **Start the helper**, as an update's but on the current image and with
   both stamps:

   ```
   docker run -d --rm --name openfit-admin-swap --label openfit.admin.helper=true
     -v /var/run/docker.sock:/var/run/docker.sock
     -v <OPENFIT_REPO>:<OPENFIT_REPO>
     -v openfit-admin_admin-data:/data
     -e OPENFIT_REPO=<OPENFIT_REPO>
     openfit-admin:local
     python /app/admin_swap.py revert <update stamp> <fresh stamp> <op id>
   ```

   As an update's, the log so far is saved, and copied into the fresh
   snapshot's `history.json`, and the operation stays running with the lock
   held until it's finished.

### What the helper does

`revert(runner, repo, data_dir, update_stamp, fresh_stamp, op_id, clock,
sleep)` in `admin_swap.py`:

1. **Wait 3 seconds.**
2. **The previous image**: `docker tag openfit-admin:prev openfit-admin:local`.
   If that fails, nothing has changed: `failed`, the Admin still on its
   version.
3. **The Admin stopped** (`stop admin`, T1), so the current one can't
   write over the settings as they go back; then **only `admin.json` and
   `users.json`** come back from the update's
   snapshot (one it doesn't have is removed). `auth.json`, `secret_key`,
   `history.json` and `last_check.json` stay as they are now: the admin
   password and its sessions survive (nobody is logged out), the activity
   log keeps everything since the update, and the last check stands.
4. **Recreate and health-check**: `up -d --no-build --force-recreate admin`,
   then the check on 8090, every 2 s for up to 60 s.
5. **Healthy**: `ok`, finished as "The Admin went back to `<short>`."
6. **Not healthy** (or it didn't start, or the settings didn't go back):
   `docker tag openfit-admin:next openfit-admin:local`, the Admin stopped
   again, all six files back from the fresh snapshot, recreate and
   health-check: `rolled_back` ("The
   previous Admin didn't come up, so this version was put back, with its
   settings as they were.") if it answers, else `failed`, with where things
   stand and the commands by hand (`docker tag openfit-admin:next
   openfit-admin:local` and the recreate, or the repo's version built).

It writes the same result file as an update, and **finishing works as an
update's**: the Admin that's running once the helper has gone (the previous
one, or the current one after a fallback) finishes the operation from it,
and it's never marked interrupted.

**An Admin from before reverts** - the first revert after S2 goes back to
S1 - can't read a revert's result: at startup it removes the file and marks
the operation interrupted. So once the previous Admin answers, the helper
looks: an operation marked so is finished by the helper itself, as the Admin
would have (its log, `ok` and the summary), and the result file removed. An
operation still running is left to the Admin that read the result.

After a revert, the Admin's code is older than the repo's `admin/`, so its
**Update available** shows again; updating moves forward as ever, and
leaves its own snapshot and `:prev`, so it can be reverted in turn.

## The app shelf (A5)

Apps reach users through a **shelf**: a Docker volume the Admin fills, which
every user's OpenFit mounts read-only at `/library`. There's no network
between containers, so user containers stay fully offline. Every app on the
shelf is available to every user; there's no allowlist. Installed apps are
separate copies in each user's `/data/apps` (B1 will copy them from the
shelf), and the user system already reloads an app whose files change, which
is what B3's self-updating will rely on. No restart is ever needed to see new
shelf contents: the mount is live.

### The volume and its layout

`admin/docker-compose.yml` has a volume `library`, `name: openfit-library`,
mounted read-write in the Admin at `/library`. Compose makes it when the
Admin is created from that file (Update the Admin does), and nothing ever
removes it.

```
/library/
  index.json
  openfit -> .sets/openfit-<commit12>      (a relative symlink)
  .sets/openfit-<commit12>/<app>/<files>
```

Each source (`openfit`, and each organisation repo's since A6) is a relative
symlink to an immutable published set. `index.json` says what's on the shelf:

```json
{"published_at": "2026-10-29T12:00:00Z",
 "sources": {"openfit": {"commit": "<40 hex>",
                         "name": "OpenFit",
                         "description": "Official applications released by OpenFit.",
                         "apps": {},
                         "published_at": "2026-10-29T12:00:00Z"},
             "openfitavi": {"commit": "<40 hex>",
                            "name": "OpenFit Aviation",
                            "description": "Aviation applications for OpenFit.",
                            "apps": {"frms": {"sha256": "<fingerprint>",
                                              "name": "FRMS",
                                              "description": "Fatigue risk management for airline crew. ...",
                                              "changes": [{"sha": "<12 hex>",
                                                           "subject": "FRMS: the rest budget counts the pre-duty time",
                                                           "date": "2026-10-29T08:30:00+00:00"}]}},
                            "published_at": "2026-10-29T12:00:00Z"}}}
```

Each source's own `published_at` (A6) is when it was last published; the
index's, when the index was last written, by any source's publish.

The fingerprint is `admin_ops.fingerprint()` of the published app folder
(each file's path and contents, dotfiles and caches left out). B1 and B3 read
the index: B3's user systems compare it with their installed copies' own,
the same function reproduced in `app/shelf.py`, to see a new version. Each
app's `changes` are its change notes (B3, below). Publishing one source
keeps any other source's entry as it is.

A source's `name` and `description` (S1) come from `library/repo.yaml`:

```yaml
name: OpenFit
description: Official applications released by OpenFit.
```

`name` text of 1 to 40 characters and `description` (optional) at most 300,
each trimmed (`repo_info()`). A file that's missing, a link, not YAML, not a
mapping, or not that, is the source's id and no description, and the publish
says why in its log. `repo.yaml` isn't an app: it isn't copied to the shelf.
Each app's `name` and `description` are its own `app.yaml`'s, read from the
published copy (`app_info()`): a non-blank name, and a description of at most
300 characters (the user system's rule), each trimmed; anything else, or an
`app.yaml` that doesn't parse, is null, and the app is published all the
same. The fingerprints don't change. The user system's shelf reader takes
only the keys it knows, so the richer index reads as before. An index an
Admin from before S1 wrote has no names; the Admin's startup publishes again
when the shelf's `openfit` entry has none, rewriting only `index.json` (the
set itself, unchanged, isn't).

### Publishing

`Admin.publish_official()` publishes the repo's `library/` at `HEAD`: the
working tree, which the Admin keeps clean. What's copied:

- **Apps**: each top-level folder named `[a-z0-9][a-z0-9-]*` is one app, at
  most 200 of them, by name. Anything else at the top level (a file, a link,
  another name) is left out and logged, and so is an app folder with no
  `app.yaml`, or one over **64 files** or **16 MB**.
- **Files**: regular files only, never through a link (a link is left out and
  logged; files are opened `O_NOFOLLOW`). Dotfiles, `__pycache__` and `*.pyc`
  are left out. Sub-folders are copied as they are.
- Nothing else is checked: the Admin has no `apps.py`, and the user system
  checks an app when it's installed (B1), as it does today.

It's atomic (`publish_set()`): the apps are copied into a hidden
`.sets/.tmp-openfit-<commit12>-<hex>`, renamed to `.sets/openfit-<commit12>`
once whole, then a new link `openfit.new` is made beside the old one and
renamed over it (`os.replace`): a reader sees the old set or the new one,
never half of either. `index.json` is written last, by a temp file and
`os.replace`. Then older sets are deleted, keeping the current one and the
one before it. A publish that fails leaves the shelf exactly as it was: the
new set deleted, the link and the index untouched (or, if it failed after the
swap, put back). Publishing the commit the shelf is already on never rewrites
its set (a set never changes); only the index, if it doesn't already say so.

Publishing never runs a docker command or a helper: it's file work through
the Admin's own mount. Every path written is under `/library`, built with
`os.path.join` from checked names, and each is resolved with
`os.path.realpath` and checked to sit inside `/library` before it's written.

### Change notes (B3)

Since B3 a library app installed in a user's OpenFit updates itself when
the shelf has a new version, and the user sees what changed
([app-host.md](app-host.md#library-apps-follow-the-shelf)). What changed is
the Admin's to say: each app's index entry carries `changes`, up to the 10
newest non-merge commits touching the app's folder, newest first, `[{sha,
subject, date}]` - `sha` the commit's first 12 hex, `subject` cut to 200
characters, `date` the committer's, ISO 8601 (`parse_changes()`). The one
git command, `change_notes_args()`:

```
git log -n 10 --no-merges --format=%H%x09%cI%x09%s <commit> -- library/<app>   (the official library)
git log -n 10 --no-merges --format=%H%x09%cI%x09%s <commit> -- <app>           (an organisation repo)
```

`<commit>` the full commit being published, `<app>` the app's folder name
as validated for publishing (`[a-z0-9][a-z0-9-]*`), always after a literal
`--`. The official library's runs as the checkout's owner in the checkout,
as every git command there does; an organisation repo's in its clone,
through `org_git()` (its allowlist has exactly this form, below).
`publish_set()` takes them as `changes(app)`; a `git log` that fails gives
that app `changes: []` and a log line ("Couldn't read library/frms's change
notes, so it's published without them."), and the publish goes on: change
notes never fail one. A publish of the commit the shelf is already on reads
them again, to compare the index; an index from before B3 has none, and the
user system takes that as `[]`.

### When it runs

1. **The Admin's startup**, before the weekly check and the server start: if
   `index.json` is missing, or its `openfit` commit isn't `HEAD`. So the shelf
   appears by itself on first rollout. A failure is logged and shown on the
   page; it never stops the Admin starting.
2. **The end of a successful Update OpenFit**, after the health checks and
   before pruning (step 7 above). A failure there doesn't undo the update.
3. **An Undo or a rollback that resets the code**: published again from the
   reset `HEAD`, so the shelf matches the code again.
4. **Update apps** (below).
5. **Publish again** (`POST /api/library/publish`), offered on the App
   library card when the shelf isn't on the version OpenFit is on: a short
   operation under the lock, in the request; a failure is a 502 with why.

### Update apps

Offered when the last check shows new commits (`behind > 0`), nothing in the
**User system** or **Admin** groups, and something in the **App library**
group (docs and the rest can come too). While it's offered, Update OpenFit
isn't: there's nothing in OpenFit to update. When OpenFit or the Admin also
changed, only Update OpenFit is offered, and it publishes the apps as part of
its run. It asks first: "Publishes the new apps to every user. Nobody's
OpenFit restarts."

`POST /api/library/update` starts it (`kind: "update_apps"`, background,
under the lock). The refusals, in the request: nothing to apply ("OpenFit
itself changed too, so use Update OpenFit: it updates the apps as well.", or
"Check for updates first."), a dirty tree, a repo not on `main`, as for
Update OpenFit. Then:

1. `HEAD` noted as `old`, then `git merge --ff-only origin/main`.
2. **What really arrived**: `git diff --name-only <old> HEAD`. If anything
   under `app/` or `admin/` came in (GitHub moved since the check, and
   something fetched it), `git reset --hard <old>` and fail: "OpenFit itself
   changed since the last check. Check again, then use Update OpenFit."
3. **Publish.** If it fails, `git reset --hard <old>` and fail, the shelf as
   it was.
4. The check refreshed, "ok": "Updated the app library to `<short>`."

No container is stopped, recreated or restarted, and no docker command runs
at all.

### The mounts

- **Added users**: the template above mounts `openfit-library:/library:ro`
  and declares the volume `external: true`.
- **The primary** gets it from an override the Admin writes into its own
  volume, `/data/primary.override.yml`:

  ```yaml
  services:
    tracker:
      volumes:
        - openfit-library:/library:ro
  volumes:
    openfit-library:
      external: true
  ```

  `compose()` passes it after the repo's file for every primary command:
  `-f <OPENFIT_REPO>/docker-compose.yml -f /data/primary.override.yml`, those
  two, in that order. The repo's `docker-compose.yml` is unchanged. The
  override is in the Admin's storage, not on a host path, so **starting the
  primary by hand** with compose in the repo (`docker compose up -d`) leaves
  the shelf out, from whenever that recreates its container until the Admin
  next recreates it (an update, or Restart).
- **At startup** the Admin writes the override if it's missing or differs,
  and rewrites every added user's compose file from the template if it
  differs. Neither restarts anyone: a user picks the mount up at their next
  recreate, an update or **Restart**.
- `GET /api/status` marks each user `library: true` or `false`, read from
  `docker inspect`: is `openfit-library` mounted at `/library`? The page shows
  "Restart to see the app library" beside a user without it.
- **The volume must be there before any recreate.** Every user's compose
  files name it as external, so recreating one without it would fail. So
  Restart, Start, Add user, Update OpenFit and Undo first run `docker volume
  inspect openfit-library`, and if it's missing refuse (409): "The Admin
  needs updating first: its app library storage isn't there yet." (It's
  missing only if the Admin wasn't recreated from its new compose file.)
  Update apps recreates nobody, so it doesn't ask.

### Rolling A5 out

1. **Update OpenFit**, which pulls this code.
2. **Update the Admin** (the button exists since A4). The new Admin's compose
   file makes the shelf's volume, and the new Admin publishes to it as it
   starts, and writes the users' mounts.
3. **Restart** each user to give them the mount. Nothing reads it until B1,
   so there's no rush.

## Organisation repos (A6)

Apps can come from repos other than OpenFit's own. Each is its own GitHub
repo, private or not, owned by whoever made it: the first is
`github.com/Raven-git-hub/openfit-aviation`, whose source is `openfitavi`.
The Admin clones it into its own storage with a **deploy key** it makes for
that repo alone, and publishes its apps to the shelf under the source the
repo declares, beside OpenFit's: every user on the server sees them in
Browse (`openfitavi.frms`, under "OpenFit Aviation"), and its updates work
as the official repo's Update apps does.

### What a repo looks like

The same as `library/`: an app folder each at its top, by the rules of
Publishing above (`[a-z0-9][a-z0-9-]*`, an `app.yaml`, at most 64 files and
16 MB, 200 apps), and beside them a `repo.yaml`:

```yaml
source: openfitavi
name: OpenFit Aviation
description: Aviation applications for OpenFit.
```

- **`source`**, required: the prefix of every app's id (`openfitavi.frms`).
  Lowercase letters and digits, starting with a letter (the user system's
  `LIBRARY_SOURCE`); never `openfit`, OpenFit's own; never one another repo
  on this server already is.
- **`name`** (1 to 40 characters) and **`description`** (at most 300),
  each trimmed: S1's rules (`repo_info()`), the source's id and no
  description when they aren't that.

Anything else at the top - a README, a licence, `.git`, `.github` - isn't an
app and isn't published (dotfiles quietly; the rest logged). A repo without
a valid `repo.yaml` - an empty repo included - can be added and connected,
but isn't published: its row says "Connected, but not an OpenFit repo yet:
it needs a repo.yaml at the top.", with why under it, and each check reads
it again.

### Adding a repo, and its deploy key

**Add repository**, on the Repo tab, asks for the repo's GitHub address:
`https://github.com/<owner>/<repo>` (with or without `.git` or a trailing
`/`) or `git@github.com:<owner>/<repo>.git`. `owner` is
`[A-Za-z0-9-]{1,39}`, `repo` `[A-Za-z0-9._-]{1,100}` and not `.` or `..`;
github.com only, and no path, query or fragment: anything else is a 400.
It's stored in its SSH form, `git@github.com:<owner>/<repo>.git`, under a
**slug**, `<owner>__<repo>` lowercased (every character outside
`[a-z0-9._-]` a `-`), which names its folder and its rows. The same repo
twice, in any case, is a 409.

Then, under the lock and in the request (`POST /api/repos {url}`), the Admin
makes it a key pair, exactly:

```
ssh-keygen -t ed25519 -N "" -C "openfit-admin <server name> <owner>/<repo>" -f /data/repos/<slug>/deploy_key
```

(`<server name>` is `docker info`'s, so GitHub's list of deploy keys says
which server each is). The folder is 0700, the private key 0600. The reply,
and the popup, give the **public key** with **Copy key** and the three
steps:

1. Open the repo on GitHub, then Settings → Deploy keys → Add deploy key.
2. Paste the key, give it a title, and leave "Allow write access" unticked.
3. Come back and press Connect.

The repo is saved as `pending` in `/data/repos.json`:

```json
{"repos": [{"slug": "raven-git-hub__openfit-aviation",
            "url": "git@github.com:Raven-git-hub/openfit-aviation.git",
            "owner": "Raven-git-hub", "repo": "openfit-aviation",
            "added_at": "2026-10-29T12:00:00Z", "state": "pending",
            "source": null, "commit": null}]}
```

Read strictly on every read, like `users.json` (`load_repos()`): exactly
those keys, every one checked (the slug the owner's and repo's, the url
their SSH form, `state` `pending` or `connected`, `source` and `commit`
null together, and only once connected, no source twice); anything else
stops the Admin starting. A pending repo shows its public key again behind
**Show key**, so it can be copied again, and keeps it until it's removed, so
nobody has to add a second one on GitHub.

**Why a deploy key.** It's read-only (the Admin never pushes), it opens one
repo and nothing else on GitHub, it's made on this server and never leaves
it, and its owner can revoke it from the repo's settings at any time. No
personal token, and nobody's GitHub account, is involved.

**What the private key never does.** It's written by `ssh-keygen` and read
only by `ssh`, through `GIT_SSH_COMMAND`'s `-i`. The Admin never opens it:
no route returns it, it's in no reply, status, history, log or settings
snapshot, and an Admin backup or download never holds it (they're of users'
volumes). Remove deletes it.

### Connect

`POST /api/repos/<slug>/connect`, an operation of kind `repo_connect`, in
the background and under the lock: `git clone -- <url> /data/repos/<slug>/repo`
with the repo's SSH settings (below).

- **GitHub turns the key down** (`Permission denied (publickey)`, or
  `Repository not found` for a key it knows for another repo): "GitHub
  didn't accept the key yet. Check it's added under the repo's Deploy keys,
  then Connect again." The repo stays pending, no clone left, its key kept.
- **GitHub out of reach**: "Couldn't reach GitHub: <git's words>", pending
  too.
- **Cloned**: the repo is `connected`. Its `repo.yaml` is read from the
  clone, never through a link, and checked: if it names a source this repo
  can be, the apps are published (below) and the source and the commit
  recorded; else it's connected and not published, and its row says why -
  not an OpenFit repo yet, or the source refused ("Its repo.yaml says it's
  'openfit', which is OpenFit's own. Choose another source in it.").

### Git over SSH, pinned

`admin/Dockerfile` installs `openssh-client`, and `admin/github_known_hosts`
- GitHub's three host keys (ed25519, ECDSA, RSA), as GitHub publishes them -
is copied into the image as `/app/github_known_hosts`. Every git command for
an organisation repo runs with exactly:

```
GIT_SSH_COMMAND="ssh -i /data/repos/<slug>/deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=/app/github_known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=20"
```

its own key and no other, GitHub's pinned keys and nothing else trusted (a
host key that isn't one of them stops the connection), never a prompt.
`ssh-keygen -lf admin/github_known_hosts` prints the fingerprints GitHub
publishes on its "GitHub's SSH key fingerprints" page:

```
256 SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU github.com (ED25519)
256 SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM github.com (ECDSA)
3072 SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s github.com (RSA)
```

When GitHub changes a key, take the new lines from `https://api.github.com/meta`
(`ssh_keys`, each prefixed `github.com `), check their fingerprints against
that page, and update the file: it reaches the image with Update the Admin.
`ssh_command()` builds it from fixed parts, the Admin's data folder and a
checked slug only (git hands it to a shell). It runs as the Admin's own user,
root, in the repo's own clone in the Admin's storage - never the server's
checkout, never as its owner - and only one of these, each in its one shape
(`org_git_allowed()`): `clone -- <its url> <its folder>`, `fetch origin`,
`rev-parse --verify -q` of `HEAD^{commit}` or `@{upstream}^{commit}`, `log`
between full commits (or the newest of one), an app's change notes (B3:
`log -n 10 --no-merges --format=%H%x09%cI%x09%s <full commit> -- <app>`,
exactly that - another count or format, no `--`, two paths, or a path that
isn't one app folder's name, and it's refused), `merge --ff-only` and
`reset --hard` of a full commit, and `cat-file` of a full commit's tree or
an object by its full id.
No HTTPS fallback and no tokens. A network that blocks port 22 to GitHub is a
later fix: GitHub's `ssh.github.com:443` route.

### Publishing a repo's apps

`publish_source(source, src_dir, commit)` is A5's publisher for any source
(`publish_official()` is `publish_source("openfit", <repo>/library, HEAD)`):
the clone's apps copied into `.sets/<source>-<commit12>`, the link
`/library/<source>` swapped onto it, `index.json` rewritten last with this
source's entry changed and every other source's exactly as it was read -
`{commit, name, description, apps, published_at}`, each source with its own
`published_at`, when it was last published (an entry from before A6 has
none, and the index's own stands for it). Publishing a repo never touches
OpenFit's link, sets or entry, nor OpenFit's a repo's. The user system
already reads every source in the index (B1), and lists each app's
`source_name`, its source's `name` there.

At the Admin's startup, after OpenFit's own catch-up, each published repo
whose shelf entry is missing or on another commit is published again from
its clone, if the clone is on the commit recorded: a shelf made anew holds
every repo's apps again.

### Checks and updates

**Check now**, and the weekly check, also run `git fetch origin` for each
connected repo, and read its `repo.yaml` again at the commit fetched
(`git cat-file`: a regular file at the top only, never a link, at most 256
KB). A repo is **behind** when `origin`'s head (its upstream) isn't the
commit it was published at; its commits since are listed. What each check
found is kept in `/data/repo_checks.json`. A fetch that fails marks that
repo alone - "Couldn't reach GitHub: <reason>" - and never fails the check,
nor the other repos; the official repo's own fetch failing doesn't stop the
repos being checked either. A repo connected while empty is cloned afresh at
each check (a fetch into an empty clone can miss the branch GitHub makes its
default), so it catches up after its first push.

The left column's **Repos** panel has a row per repo, OpenFit first and the
rest by name, each with its version, when it was last published and, when
it's behind, **Update available**. Its update popup's Proceed runs
`repo_update` (`POST /api/repos/<slug>/update`, background, under the lock):
`merge --ff-only` of what the check fetched, `repo.yaml` checked again,
publish, and the new commit recorded. A step that fails - not a
fast-forward, a `repo.yaml` that no longer passes, a publish that fails -
puts the clone back (`reset --hard <old>`) and leaves the shelf untouched.
No container is stopped, recreated or restarted: no docker command at all.

**A repo whose `source` changes** in its `repo.yaml` is refused: "This repo
now says it's '<new>', but it was added as '<old>'. Remove and add it again
to change it." - at the check, which offers no update, and at the update,
which publishes nothing. A changing prefix would cut installed apps off
from their data (`app:openfitavi.frms` stays `openfitavi`'s).

### Removing a repo

**Remove**, on its row, asks first: "Its apps leave the app library. Users
who installed them keep them, but they won't be updated. Remember to delete
the deploy key on GitHub too." Then `POST /api/repos/<slug>/remove`, under
the lock, in the request: its source off the shelf (`unpublish_source()`:
its link removed, then its sets, then its `index.json` entry, last - every
other source's exactly as it was), then `/data/repos/<slug>/` deleted - the
clone and the key - then its `repos.json` entry. Installed copies in users'
`/data/apps` aren't touched: the Admin never reaches into a user's volume.
A step that fails stops there, with the repo still listed, to remove again.

## The safety rules

The Admin holds the docker socket, so it has full control of the host. A
shared server runs other things (n8n, Open WebUI, Music Assistant,
Tailscale sidecars and more), and the Admin must never touch any of them.

- **Its own users' projects only.** Every compose command is one of two
  shapes (and a restore's create, R1, is `up --no-start --no-build` on a
  new user's project): `docker compose -p openfit -f <OPENFIT_REPO>/docker-compose.yml -f
  /data/primary.override.yml …`, run in `OPENFIT_REPO`, for the primary
  (exactly those two files, in that order); or `docker compose -p
  openfit-<name> -f /data/users/<name>/docker-compose.yml …`, for a name in
  `users.json`. The repo's compose file is only ever used with `-p openfit`,
  and `compose()` refuses any other pairing (a test builds a `User` breaking
  each rule and checks nothing runs).
- **Its own project only to update itself.** The `openfit-admin` project is
  only ever `docker compose -p openfit-admin -f
  <OPENFIT_REPO>/admin/docker-compose.yml`, run in `<OPENFIT_REPO>/admin`,
  and only `build admin` (the running Admin), or `stop admin` and `up -d
  --no-build --force-recreate admin` (the helper: a stop just before it puts
  settings back, always followed by the up that starts it again - the lane
  test holds a stop to the helper's swap or revert, and to an up after it),
  through `admin_compose()`, which refuses anything else. Its own container
  is found by its labels, health-
  checked and inspected for its image; the helper is looked for by its name,
  `openfit-admin-swap`, and started exactly in one of its two forms: an
  update's, on `openfit-admin:prev`, or a revert's (S2), on
  `openfit-admin:local` (`swap_helper_args()`, which refuses anything else).
  Its image is tagged only among `openfit-admin:{local,prev,next}`, through
  `admin_tag()`, which allows exactly four: the running image (by its id)
  kept as `:prev` (an update) or `:next` (a revert), and `:prev` or `:next`
  put back as `:local` (the helper). `docker image inspect` of
  `openfit-admin:prev` tells the revert's offer it's there. Never `down`,
  `-v`, `rm` or a volume on it.
- **Its own containers only.** Every docker command targets one of: a
  container compose labels as one of those projects' `tracker` service (not a
  one-off `compose run`), found by `docker ps -a` with those label filters,
  where the only commands it execs are the health check (`python -c` and
  the fixed code) and a setup link's `python auth_cli.py setup-link`; a
  helper container the Admin starts itself, labelled
  `openfit.admin.helper=true`, always `--rm`, from its own image (the
  backup helpers, the update helper above, and the kept-data download's,
  in its one form, `kept_download_args()`: the removed user's volume
  read-only, the password's two files excluded - or, packing it for a
  restore, the same form with nothing excluded, `kept_pack_args()`; and a
  restore's, R1, in its one form, `restore_helper_args()`: the new user's
  volume read-write, the Admin's read-only, and `tar xzf` of a clean
  archive the Admin wrote, by a name only its pattern matches);
  `docker image inspect openfit-tracker:latest`; `docker volume inspect` of
  the volume a user being added would get, and of the shelf's,
  `openfit-library`, before anything that recreates a user; and `docker volume rm
  openfit-<name>_tracker-data`, for a name in `removed`, under Delete data's
  confirm. The tags it sets are `openfit-tracker:prev` and
  `openfit-tracker:latest`. Beside those, the System information panel's
  `docker info --format '{{json .}}'` (that format exactly; at most once a
  minute), of which only the server's name, Docker version, CPUs and memory
  are kept. Nothing else is listed, inspected, stopped or removed; the Admin
  doesn't list other containers, even to find free ports.
- **Data deleted in two places only.** `down -v` only in a failed add or a
  failed restore (R1), on a volume that operation made; `docker volume rm`
  only in Delete data. Remove is
  always a plain `down`. The shelf's volume is never removed: the users'
  files name it `external`, so not even a failed add's `down -v` touches it.
- **The shelf is written as files, never through docker.** Publishing is
  file work through the Admin's own `/library` mount: no docker command and
  no helper (a test fails any docker call made from a publishing function).
  Every path it writes is under `/library`, built from checked names and
  each resolved (`os.path.realpath`) and checked inside the shelf before
  it's written; nothing in the repo is followed through a link.
- **No shell.** Every command is an argument list, run through one `Runner`
  (`admin_ops.py`) with `subprocess.run`, never a shell. What a person types
  reaches a command only as a user's name or port, after the rules above. The
  rest is fixed strings, the settings and `users.json` (checked against
  strict patterns on every read), and ids that git and docker gave back
  (checked before use). The one `sh -c` is Undo's restore helper's fixed
  string, inside a container. A test reads every `admin/*.py` for `shell=True` and
  `os.system`. A download's helper runs through `Runner.stream()`, an
  argument list too.
- **No archive extracted by the Admin** (R1). A restore reads an archive
  with `tarfile` and writes a clean copy of the members it checked; the
  Admin process never extracts one. Only the restore helper extracts, and
  only that clean copy, into a volume made moments before.
- **Git as the owner.** Git runs as the uid and gid that own `OPENFIT_REPO`,
  with `HOME=/tmp` and no supplementary groups, so the files `git merge`
  writes stay owned by `docker` (your own `git pull` keeps working) and git's
  "dubious ownership" refusal never fires. Docker runs as root.
- **An organisation repo's git in its own lane** (A6). As root, in its own
  clone under `/data/repos/<slug>/`, never the server's checkout; always
  with its pinned `GIT_SSH_COMMAND` (its key, GitHub's host keys,
  `StrictHostKeyChecking=yes`, `BatchMode=yes`); only `clone`, `fetch
  origin`, `rev-parse`, `log` (a span, the newest of a commit, or an app
  folder's change notes, B3), `merge --ff-only`, `reset --hard` and
  `cat-file`, each in its one shape (`org_git()` refuses anything else,
  running nothing). `ssh-keygen` only ever in its one form, making a repo's
  key. A test holds every scenario to both, written out by hand.

## Where its data lives

The Admin's volume, `openfit-admin_admin-data` (its name fixed in the
compose file, because the backup helpers mount it by name), at `/data`:

| File | What |
| --- | --- |
| `admin.json` | The settings, written with the defaults on first start: `{"user": "andy", "project": "openfit", "service": "tracker", "port": 8080, "branch": "main"}`, the primary user. No UI changes them. |
| `users.json` | The added users and the removed ones whose data is kept (above). |
| `users/<name>/docker-compose.yml` | Each added user's compose file, from the template; rewritten at startup if it differs. |
| `primary.override.yml` | The primary's mount of the shelf (A5), passed after the repo's compose file; rewritten at startup if it differs. |
| `auth.json` | The password hash (0600). |
| `secret_key` | The session key (0600). |
| `last_check.json` | The last check, and the last failed one's error. |
| `history.json` | The last 20 operations, with their logs. |
| `backups/` | `<UTC stamp>-<old sha>-<user>.tar.gz`, the newest 3 sets kept. Read-only on the page. |
| `self-update/<stamp>/` | An Admin update's settings snapshot, or a revert's fresh one: the six files above it, each if present, for a rollback, a revert or its fallback to put back. The newest 3 kept, and always the one a revert is putting back. |
| `self_update_result.json` | The update or revert helper's log while it runs, `{op_id, stamp, outcome, lines}`; deleted once an Admin (or, for an Admin from before reverts, the helper) has finished the operation from it. |
| `repos.json` | The organisation repos added (A6), pending or connected, each its source and published commit once it has them (above). |
| `repos/<slug>/` | A repo's deploy key pair (`deploy_key`, 0600, and `deploy_key.pub`) and its clone (`repo/`); the folder 0700, deleted by Remove. |
| `repo_checks.json` | What the last check, connect or update found of each repo: GitHub's head, the commits since, a `repo.yaml` problem, a failed fetch. |
| `restore-uploads/` | A restore's own files (R1), 0700: an upload, a removed user's kept data packed, the database's copy checked and the clean archive the helper extracts. Deleted as each restore ends, and at startup. |

Each user's own data is their volume: `openfit_tracker-data` for the primary,
`openfit-<name>_tracker-data` for an added user. The app shelf is the volume
`openfit-library` (A5), the Admin's `/library` and every user's, read-only.

## Try it

For R1, on the server, once Update OpenFit and then Update the Admin have
run:

1. Under User data, open a backup and **Download** it (the file is
   `…-nopassword.tar.gz`).
2. In Add user, a new name, the suggested port, and **Restore from a
   file**: the downloaded file. The button reads **Restore user**; leave
   Keep device logins ticked and press it. The Activity log shows the checks
   ("It holds N files and folders, and its database is sound, at migration
   10."), what was left out, then "Restored <name>. They start locked: use
   Setup link to give them a password."
3. **Setup link** in that result: open it, set a password, and their data is
   there, as the backup had it.
4. Open the same backup again, **Restore as a new user**: with Keep device
   logins ticked the form warns that the backup's user is still a user here;
   untick it and Restore under another name. That user's devices need
   removing and re-adding.
5. Remove that test user and Delete data, as for any user.

To move someone to another server: Download their backup (or their kept
data, once removed) here, and Restore from a file there.

For A6, on the server:

1. Check now, Update OpenFit, then Update the Admin (the new Admin image has
   `openssh-client` and GitHub's host keys). FRMS leaves the official
   library: the Repo tab shows OpenFit with no apps, and Browse no FRMS for
   now.
2. Fill the aviation repo: clone `openfit-aviation`, copy
   `tests/fixtures/apps/frms/` from OpenFit into a top-level `frms/`
   folder, add the `repo.yaml` above, commit and push.
3. In the Admin: Repo, **Add repository**, paste
   `https://github.com/Raven-git-hub/openfit-aviation`, **Copy key**, add it
   on GitHub as a deploy key (read-only), then **Connect**. The Activity log
   says "Connected Raven-git-hub/openfit-aviation, and published 1 app to
   the app library as 'openfitavi'."
4. In OpenFit, Browse lists FRMS under "OpenFit Aviation", saying you have
   your own FRMS, to uninstall it and delete its data first (B2, moving it
   across, is parked).

For S2: Check now, Update OpenFit, then Update the Admin. Revert is on
offer straight away, under Versions, in the Admin's last update: it takes the
Admin back to S1 (the update that installed S2 left a snapshot and a `:prev`
like any other). Revert, and the page reconnects on S1, still logged in, the
activity log whole, with "The Admin went back to `<sha>`." Then Update the
Admin to come forward again. Under User data, open a backup: Download shows
the warning and then the file, `…-nopassword.tar.gz`; `tar tzf` on it lists
`./tracker.db` and no `./auth.json` or `./.session_key`. Delete a backup of
the last update's set, and Undo is no longer offered.

For S1, the portal: Check now shows changes to the Admin, the App library and
Other. Update OpenFit (the User's Update available): it pulls the new code
and builds, and the Admin it runs on, still the old one, publishes the shelf
as it always has. Then Update the Admin (its Update available, under
Versions); after the page reconnects it's the portal, and its startup
publishes the shelf's index again with `repo.yaml`'s name and blurb and the
apps' names (no new set). The Repo tab shows OpenFit, "Official applications
released by OpenFit.", and FRMS with its description.

Setup links (U1), once Update OpenFit and then Update the Admin have run:
every OpenFit shows "locked". Press **Setup link** on your row, open the
link, set a password and you're signed in. Press it again and open the new
link in another browser: setting a password there signs the first browser
out. A stopped user's button is greyed out (the route says 409).

On the server, once the Admin is rebuilt with A2: open the Admin, add a test
user (say `test`, on the suggested port), open their address and see a fresh
OpenFit, then Remove them, and Delete data under User data by typing `test`.

For A4: install it the old way once (Update OpenFit, then the command the
note shows). The next time an Update OpenFit brings Admin changes, the note
shows **Update the Admin**: the page goes away for under a minute, comes
back by itself, and Activity reads "The Admin updated itself to `<sha>`."

For A5: Update OpenFit, then Update the Admin. The App library card (S1's
Repos panel) shows the shelf published from the version OpenFit is on, with
its apps. Restart a user, and the "Restart to see the app library" hint
beside them goes. The next app-only change on GitHub shows **Update apps**
in place of Update OpenFit, and nobody's OpenFit restarts.
