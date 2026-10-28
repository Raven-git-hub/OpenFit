# The OpenFit Admin (A1, A2, A4, A5, U1): updates behind buttons, users, its own update, the app shelf and setup links

_Built: A1, A2, A4 and A5, slices of the Admin in
[distribution.md](distribution.md), and U1's setup links (the user login,
[auth.md](auth.md)). The code is `admin/`; the tests are
`tests/test_admin_*.py`._

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

## What the Admin doesn't do yet

- Rename a user, or change a user's port after it's added. Users on
  different versions.
- Update OpenFit and update itself in one go: Update OpenFit pulls the
  Admin's new code, and Update the Admin is a second button.
- Self-updating installed apps (B3); organisation repos (A6); `replaces`
  moving an app's data (L3, built as B2) or host-version labels (L4). Browse,
  installing an app from the shelf, is the user system's (B1).
- Scheduled updates: an update runs now, or is found by the weekly check and
  left for a person.
- Fetch anything from GitHub but the repo itself: pull request numbers are
  read from merge commit titles and linked, never fetched.
- The proxy and `.local` names. OpenFit and the Admin are reached at
  `IP:port`.
- The backup file format. An Admin backup is a plain `tar.gz` of a user's
  `/data` volume.

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
   says so ("The update itself is done. Use Publish again, on the App
   library card, …"), and the page's App library card shows "The app library
   couldn't be updated" with **Publish again**. Then backups are pruned to
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
pruned.

## The page

- **Users**: the version everyone is on, then a row per user: name ("you"
  beside the primary), `http://<host>:<port>` as a link, state (Running,
  Stopped, Not found), with "Restart to see the app library" under it for a
  container without the shelf's mount, Start or Stop, Restart, and Remove
  (not for the primary, with its confirm). **Add user**: a name, a port prefilled with the suggestion,
  and Add; the progress shows under Activity. **Kept data**: each removed
  user with Delete data, which asks for their name.
- **Updates**: the check, Update OpenFit and Undo last update, as above; with
  only the app library changed, a line pointing to Update apps.
- **App library** (A5): when the shelf was published and from which version,
  the apps on it by folder name, **Update apps** when it's offered (with its
  confirm), and, when the shelf isn't on the version OpenFit is on (a publish
  failed), "The app library couldn't be updated", why, and **Publish again**.
- **An Admin update is available**: only when there is one, with Update the
  Admin when it can run (below).
- **Activity**: the running or last operation's log, and the last few.
- **Backups**: each with its user, name, size and when.

`GET /api/status` carries `users: [{name, port, primary, state, library}]`
(state `running`, `stopped` or `missing`; `unknown` if Docker didn't answer;
`library` whether its container has `openfit-library` mounted at `/library`,
read from `docker inspect`), `removed: [{name, removed_at}]`, `next_port`,
`update_apps_available` and `library: {published_at, commit, short, apps,
current, error}`, beside A1's fields. Every
state-changing call goes through the page's `fetch` wrapper (the CSRF header)
and needs a session.

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
  (`can_self_update`): the note "An Admin update is available" says its new
  files are already on the server and shows **Update the Admin**. The command
  to run by hand stays under it, smaller, as a fallback:

  ```
  cd ~/containers/openfit && docker compose -f admin/docker-compose.yml -p openfit-admin up -d --build
  ```

- **Admin commits on GitHub not pulled yet**: the note lists them and says
  "Update OpenFit first: it brings the Admin's new code with it." No button.
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
`admin_compose()` (`admin_ops.py`), which runs only `build admin` and `up -d
--no-build --force-recreate admin` and refuses anything else, and it's called
from exactly two places: step 5, and the helper.

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
   `docker tag openfit-admin:prev openfit-admin:local`; the snapshot restored,
   each file in `/data/self-update/<stamp>/` copied back over `/data/`, and any
   of the six names the snapshot doesn't have removed; `up -d --no-build
   --force-recreate admin` again, and the health check. The result is
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

Update the Admin asks first: "The Admin restarts on its new version. This
page will be unavailable for up to a minute and reconnects by itself. If the
new version doesn't start, the old one is put back." Then the page shows the
operation's log. When its requests start failing, it shows "The Admin is
restarting…" and polls `GET /` every 2 seconds; once that answers, it
reloads, and the finished operation appears in Activity. The login session
survives, because `secret_key` is unchanged; if a rollback put back an older
`auth.json`, the page simply shows the login.

### Installing A4: the manual command one last time

The Admin running before A4 has no `admin_swap.py` in its image, and the
helper always runs the current image. So A4 itself is installed the old way,
once: **Update OpenFit**, then run the command the note shows on the server.
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

Each source (`openfit` now, organisation repos in A6) is a relative symlink
to an immutable published set. `index.json` says what's on the shelf:

```json
{"published_at": "2026-10-27T12:00:00Z",
 "sources": {"openfit": {"commit": "<40 hex>",
                         "apps": {"steps": {"sha256": "<fingerprint>"}}}}}
```

The fingerprint is `admin_ops.fingerprint()` of the published app folder
(each file's path and contents, dotfiles and caches left out). B1 and B3 read
the index. Publishing one source keeps any other source's entry as it is.

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

## The safety rules

The Admin holds the docker socket, so it has full control of the host. A
shared server runs other things (n8n, Open WebUI, Music Assistant,
Tailscale sidecars and more), and the Admin must never touch any of them.

- **Its own users' projects only.** Every compose command is one of two
  shapes: `docker compose -p openfit -f <OPENFIT_REPO>/docker-compose.yml -f
  /data/primary.override.yml …`, run in `OPENFIT_REPO`, for the primary
  (exactly those two files, in that order); or `docker compose -p
  openfit-<name> -f /data/users/<name>/docker-compose.yml …`, for a name in
  `users.json`. The repo's compose file is only ever used with `-p openfit`,
  and `compose()` refuses any other pairing (a test builds a `User` breaking
  each rule and checks nothing runs).
- **Its own project only to update itself.** The `openfit-admin` project is
  only ever `docker compose -p openfit-admin -f
  <OPENFIT_REPO>/admin/docker-compose.yml`, run in `<OPENFIT_REPO>/admin`,
  and only `build admin` (the running Admin) or `up -d --no-build
  --force-recreate admin` (the helper), through `admin_compose()`, which
  refuses anything else. Its own container is found by its labels, health-
  checked and inspected for its image; the helper is looked for by its name,
  `openfit-admin-swap`, and started exactly as above. The tags it sets are
  `openfit-admin:prev` (the running image) and, in a rollback,
  `openfit-admin:local` (put back from `:prev`). Never `down`, `-v`, `rm` or a
  volume on it.
- **Its own containers only.** Every docker command targets one of: a
  container compose labels as one of those projects' `tracker` service (not a
  one-off `compose run`), found by `docker ps -a` with those label filters,
  where the only commands it execs are the health check (`python -c` and
  the fixed code) and a setup link's `python auth_cli.py setup-link`; a
  helper container the Admin starts itself, labelled
  `openfit.admin.helper=true`, always `--rm`, from its own image (the
  backup helpers, and the update helper above);
  `docker image inspect openfit-tracker:latest`; `docker volume inspect` of
  the volume a user being added would get, and of the shelf's,
  `openfit-library`, before anything that recreates a user; and `docker volume rm
  openfit-<name>_tracker-data`, for a name in `removed`, under Delete data's
  confirm. The tags it sets are `openfit-tracker:prev` and
  `openfit-tracker:latest`. Nothing else is listed, inspected, stopped or
  removed; the Admin doesn't list other containers, even to find free ports.
- **Data deleted in two places only.** `down -v` only in a failed add, on a
  volume that add made; `docker volume rm` only in Delete data. Remove is
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
  (checked before use). The one `sh -c` is the restore helper's fixed string,
  inside a container. A test reads every `admin/*.py` for `shell=True` and
  `os.system`.
- **Git as the owner.** Git runs as the uid and gid that own `OPENFIT_REPO`,
  with `HOME=/tmp` and no supplementary groups, so the files `git merge`
  writes stay owned by `docker` (your own `git pull` keeps working) and git's
  "dubious ownership" refusal never fires. Docker runs as root.

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
| `self-update/<stamp>/` | An Admin update's settings snapshot: the six files above it, each if present, for a rollback to put back. The newest 3 kept. |
| `self_update_result.json` | The update helper's log while it runs, `{op_id, stamp, outcome, lines}`; deleted once an Admin has finished the operation from it. |

Each user's own data is their volume: `openfit_tracker-data` for the primary,
`openfit-<name>_tracker-data` for an added user. The app shelf is the volume
`openfit-library` (A5), the Admin's `/library` and every user's, read-only.

## Try it

Setup links (U1), once Update OpenFit and then Update the Admin have run:
every OpenFit shows "locked". Press **Setup link** on your row, open the
link, set a password and you're signed in. Press it again and open the new
link in another browser: setting a password there signs the first browser
out. A stopped user's button is greyed out (the route says 409).

On the server, once the Admin is rebuilt with A2: open the Admin, add a test
user (say `test`, on the suggested port), open their address and see a fresh
OpenFit, then Remove them, and Delete data under Kept data by typing `test`.

For A4: install it the old way once (Update OpenFit, then the command the
note shows). The next time an Update OpenFit brings Admin changes, the note
shows **Update the Admin**: the page goes away for under a minute, comes
back by itself, and Activity reads "The Admin updated itself to `<sha>`."

For A5: Update OpenFit, then Update the Admin. The App library card shows
the shelf published from the version OpenFit is on, with its apps. Restart a
user, and the "Restart to see the app library" hint beside them goes. The
next app-only change on GitHub shows **Update apps** in place of Update
OpenFit, and nobody's OpenFit restarts.
