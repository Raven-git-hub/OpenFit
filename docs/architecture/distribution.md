# OpenFit distribution: the library, the Admin and the user system

_Design record · decided 2026-10-27. Built so far: the library folder (L1,
corrected in B1: FRMS alone; empty since A6, FRMS moved to the aviation
repo), library app ids with read-only library apps (L2), five Admin slices:
updates for the existing OpenFit (A1), adding users, with updates moving
everyone together (A2), the Admin's self-update (A4), the app shelf (A5)
and organisation repos (A6, all five [admin.md](admin.md)), Browse (B1),
the user system installing from the shelf, the user login (U1,
[auth.md](auth.md)): one account per user container, set through setup
links from the Admin, the Admin Portal (S1, [admin.md](admin.md)): the
Admin's page in OpenFit's style, S2: reverting the Admin, downloads
without the password and deleting a backup, B3: installed library
apps updating themselves when the shelf has a new version, and T1, a
tidy-up of five loose ends (below). B2 is parked (below). Everything else
here is designed, not yet in the code._

OpenFit is splitting into three layers: the repo, an **Admin** container that
manages user containers on a server, and the **user system** (today's `app/`).
Apps such as FRMS are no longer part of the user system. They live in a
**library** the Admin serves, and a user installs them from Browse. This
records how the three fit together, so each slice is built against one design
rather than its own guess. Until a slice lands, the code is as `CLAUDE.md`
describes it: one account per user container, signed in with a password
set through a setup link (U1), and an Admin that adds users, updates them
all together (A1, A2), updates itself and reverts that (A4, S2), makes
their setup links (U1), publishes the official apps to a read-only shelf
every user mounts (A5) - and each organisation repo's beside them (A6) -
which a user browses and installs from (B1) and whose new versions each
installed library app takes up by itself (B3), and hands over a backup or
a removed user's data without its password (S2), and nothing more.

## Layers

- **The repo.** One repo holds `app/` (the user system), `library/` (the
  official apps, none today, and `repo.yaml`, their name and blurb) and
  `admin/` (the Admin, six of its slices built). The official repo is
  public. An organisation's app repo is separate and private, and the Admin
  reads it with a read-only deploy key it makes for that repo alone (A6,
  built: below).
- **The Admin.** One container per server. It creates users, checks for
  updates, applies them and serves the app library, as a read-only shelf: a
  volume it fills, which every user's container mounts (A5). It is the only
  OpenFit component that talks to the internet (GitHub), and only to check
  for updates or pull them - the official repo over HTTPS, as the server's
  checkout always has, and each organisation repo over SSH with its deploy
  key (A6).
- **The user system.** One container per user, with its own `/data` volume.
  It is fully offline apart from its own sync plugins' calls to their device
  APIs, as it is today.

## Updates

- **Checks** run automatically, weekly, or when the button is pressed. They
  are reported separately for the user system, the admin system and the app
  repository, each with its release notes. A change is classified by the
  folder it's in, so an app change never triggers an image rebuild: the
  library is outside the image (below). An app-only change goes out with
  **Update apps**, with no rebuild and no restarts (A5).
- **Updating is always manual.** The user system image is built once per
  version, tagged by commit. Each user's data is backed up, then their
  container is recreated on the new image. The previous tag is kept, for
  rollback.
- **The Admin updates itself** through a short-lived helper container: stop
  the old Admin, start the new one, health-check it, roll back if it's
  unhealthy, exit (A4, built: below).
- **The Admin only ever acts on containers it has labelled as OpenFit's.** A
  server is shared with unrelated services, and those are never touched.

### Updates for the existing OpenFit (A1, built)

The first Admin slice, in `admin/` (its own image and compose file, on port
8090), puts the old SSH update (`git pull && docker compose up -d --build`)
behind buttons for the one OpenFit already running. It adopts that compose
project in place, with the same volume, port 8080 and project name. A check
fetches and reports what's new by group (user system, Admin, app library,
other), with the merged PRs linked; it runs on a button and weekly. An update
refuses a dirty tree, keeps the old image as `:prev`, pulls (fast-forward
only), builds while the old version runs, stops it, backs up `/data` with a
labelled `--rm` helper, starts the new version and health-checks it, rolling
back the data, image and code itself if it doesn't come up; Undo does the same
by hand. The Admin's own update was a command you ran, until A4 (below). The full
record, with its safety rules (one compose project, the labelled container
and its own helpers only, no shell, git as the repo's owner), is
[admin.md](admin.md).

### Adding users (A2, built)

The second slice lets the Admin add users, each with their own OpenFit: a
compose project `openfit-<name>`, a volume of their own and a port of their
own (the lowest free from 8101 suggested). The existing OpenFit is the
**primary** user, adopted as in A1; an added user's compose file is written by
the Admin into its own storage from a fixed template (the image, a port and a
volume, with no `env_file` and no `environment`, so the repo's `.env`, which
can hold the primary's logins and secret key, never reaches them). Users are
started, stopped and removed from the page; a removed user's data is kept
until it's deleted by typing their name. **Update and Undo move every user
together**, one version on the server at a time: one build, everyone who was
running stopped, everyone backed up (one backup set), everyone recreated on
the new image as they were (a stopped user stays stopped), the running ones
health-checked, and everyone rolled back if anyone doesn't come up. Undo is
offered only while the set of users is the one the update recorded. "OpenFit's"
now means the compose labels of the Admin's own users' projects; the rules are
[admin.md](admin.md)'s.

### The Admin's self-update (A4, built)

**Update the Admin** replaces the command the Admin's note used to show. It's
offered once Update OpenFit has pulled the Admin's new files (one repo, one
pull), and refused while anything else runs. The running Admin keeps its
image as `openfit-admin:prev`, snapshots its settings files into its own
volume, builds the new image while it keeps running, and hands the swap to a
short-lived helper container (`admin_swap.py`) that runs `:prev`, the
version known to work: a broken new version can only fail the health check,
never run the swap or the rollback. The helper recreates the Admin on the
new image and health-checks it; if it doesn't answer, it retags `:prev` as
`:local`, puts the settings snapshot back and recreates the old one. Its log
goes to a result file as it runs, which the Admin running afterwards turns
into the operation's outcome. Every compose command on the Admin's own
project goes through one function and is one of two (`build admin`, `up -d
--no-build --force-recreate admin`); no user's project is touched. A4
itself is installed with the manual command one last time.

### The Admin Portal (S1 and S2, built)

The Admin's page, redrawn in OpenFit's style: OpenFit's theme tokens and
components, copied and held to OpenFit's by a drift test, under OpenFit's
header with ADMIN PORTAL beside the brand. A left column of four panels
that are always there: **System information** (the server's name, Docker
version, CPUs and memory from `docker info`, of which nothing else is kept;
the free disk, the backups, the users and when the Admin started),
**Versions** (the users' and the Admin's, when each was last updated, what
the last update brought, and Update available opening one update popup),
**Repos** (each shelf source, its version and last publish) and the
**Activity log**. On the right, two tabs: **Users** (the users, Add user,
User data - backups and removed users' kept data in one list - and what
everyone runs) and **Repo** (each source and its apps, with their
descriptions). The library's `repo.yaml` names a source on the shelf, and
`index.json` gains each source's and app's name and description. A
re-layout and a few reads: every route, operation and safety rule is as it
was. **S2** (built) wires up the places S1 left disabled. **Revert the
Admin** takes it back to the version before its last update: offered only
while that update's settings snapshot and `openfit-admin:prev` are still
there, the users are the ones it had and the Admin isn't on `:prev`
already; the same hand-over as its update, the other way round (a helper
on the image running now, `:next` and a fresh snapshot to fall back on),
putting back only `admin.json` and `users.json`, so the password, sessions
and activity are kept. **Download** a backup or a removed user's kept
data, never with the password in it (below). **Delete** one backup, which
takes Undo away if Undo needed it.

### The app shelf (A5, built)

**Delivery is a read-only shelf, with no allowlist.** The Admin's compose
file has a volume, `openfit-library`, which the Admin fills through its own
mount at `/library` and every user's OpenFit mounts read-only at `/library`:
the primary through an override the Admin writes, `/data/primary.override.yml`,
passed after the repo's compose file (which doesn't change); an added user
through their template. There's no network between containers, so user
containers stay fully offline, and every app on the shelf is available to
every user. The official apps (the repo's `library/`) are published as an
immutable set, `.sets/openfit-<commit12>`, behind a relative symlink
`openfit`, swapped in one step so a reader sees the old set or the new, never
half; `index.json`, written last, says each source's commit and each app's
fingerprint, for B1 and B3 to read. Publishing is file work, no docker
command; a publish that fails leaves the shelf as it was. It runs at the
Admin's startup when the shelf is behind the code, at the end of Update
OpenFit, after an Undo or rollback, and in **Update apps**, offered in place
of Update OpenFit when only the app library changed: pull, check nothing
under `app/` or `admin/` arrived, publish, with no container stopped,
recreated or restarted. The mount is live, so new shelf contents need no
restart; a user picks the mount itself up at their next recreate, an update
or **Restart** (new in A5, per user). Browse (B1, below) reads it.

### Organisation repos (A6, built)

**An organisation's apps come from its own repo**: a private GitHub repo,
owned by whoever made it, laid out as `library/` is (a `repo.yaml` at its
top - `source`, `name`, `description` - and one folder an app). The first
is `github.com/Raven-git-hub/openfit-aviation`, source `openfitavi`. In the
Admin Portal's Repo tab, **Add repository** takes the repo's GitHub address
and makes an ed25519 key pair for that repo on that server alone; the page
shows the public key, to add on GitHub as a read-only deploy key, and
**Connect** clones the repo over SSH into the Admin's own storage
(`/data/repos/<owner>__<repo>/`) and publishes its apps to the shelf under
the source its `repo.yaml` names: `.sets/openfitavi-<commit12>`, a link
`openfitavi`, and its own entry in `index.json`, beside OpenFit's and never
touching it. Every user sees its apps in Browse, named by `repo.yaml`'s
`name` (`source_name`). **Check now** and the weekly check fetch every
connected repo too, and re-read its `repo.yaml` at what GitHub has; a repo
that can't be reached is marked on its own row and never fails the check.
Its update is **Update available** on its row: a fast-forward, the
`repo.yaml` checked again, a publish, no restarts - or the clone put back
and the shelf as it was. A repo whose `repo.yaml` changes its `source` is
refused (a changing prefix would cut installed apps off from their data):
remove it and add it again. **Remove** takes its apps off the shelf and
deletes the clone and the key; what users installed from it stays theirs,
no longer updated. Git over SSH is pinned: GitHub's own host keys, the
repo's key alone, no prompt; no HTTPS and no tokens.

**FRMS moved there.** It left the official library in A6: `library/` holds
`repo.yaml` alone, and publishes an empty set; the copy the tests run
against is `tests/fixtures/apps/frms/`, byte-identical. In the aviation
repo, FRMS is `openfitavi.frms` - its `app.yaml` unchanged, `replaces:
[frms]` and all. B2 would have moved the owner's data from their own
`frms` to `openfitavi.frms`, but it's parked: the owner removed their
`frms` and its data and installed FRMS fresh from Browse, so there's
nothing to move. `replaces` stays parsed and refused while what it
replaces, or its data, is here - Browse says so and won't install it -
until a move is needed.

## Apps

- **The library** holds the official apps plus each organisation repo's. Any
  user can install any library app; there is no allowlist. It reaches users
  as the read-only shelf (A5, above), each repo a source of its own on it
  (A6, above).
- **Names.** A library app is named `<source>.<name>`: `openfit.` for an
  official app, `openfitavi.` for the example aviation repo's. **The repo
  assigns the source, never the app** (decided, and built in L2, below): a
  library app's own `app.yaml` keeps its bare id (`id: frms`), and the source
  is added at install. So nobody can publish an app claiming to be
  `openfit.`, and moving an app between repos never touches its files. A
  `replaces` field lets a renamed or moved app carry its data across: the next
  slice (L3, below). The one question still open for it is how `replaces`
  moves the data, and L3's rules for that are agreed.
- **Host version.** A library app carries a host-version requirement, and
  the host refuses an install it isn't compatible with. Built in B1 as an
  **app level**: a manifest's `requires` (1 when it doesn't say) against the
  user system's `APP_LEVEL` (1), raised when apps gain something an older
  OpenFit lacks. An app needing more doesn't load, won't install, and shows in
  Browse as needing a newer OpenFit. L4's host-version labels build on it.
- **Library apps are the Admin's to update.** The Admin publishes a library
  app's updates, and every installed copy takes them up by itself. A user
  can't edit a library app (Duplicate gives them a custom copy to change),
  and sees an "updated, what changed" note when one is updated. The
  read-only half is built (L2, below), the following and the note too
  (B3, below).
- **A user's custom apps are theirs.** They never leave the user's
  container, and the Admin never sees them.

The library starts as `library/`, at the repo's top level: the apps OpenFit
publishes, one folder an app, named for its id. It's outside the image's build
context (the compose file builds `./app`), so it's never copied into the user
system: an app reaches a user's `/data/apps` only by being installed.
`tests/test_library.py` holds the three things that make it a library: it
sits outside the build context, every folder is named for its app's id, and
every app installs through the host's own `install_app()` from its files as
text. It's named `library/`, not `apps/`, so it can't be confused with `app/`.

**Corrected in B1: the library is FRMS alone.** L1 moved eight apps into
`library/`, but seven of them (`caffeine`, `import`, `mood`, `steps`,
`steps-metres`, `weight`, `workout-log`) were leftovers from testing, never
official apps, and once the shelf existed (A5) it published them to every
user. B1 moved them, byte-identical, to `tests/fixtures/apps/`, where the
host's tests run against them as before. `library/` held only `frms` -
until A6 moved FRMS to the aviation repo (above), and its copy here to
`tests/fixtures/apps/frms/`. `library/` is `repo.yaml` alone today.

### Library app ids (L2, built)

The host side of the names, the read-only rule and a function that installs
from a library, which Browse's install calls (B1, below).

- **Two id shapes.** A custom app's id is the scanner's `APP_ID`, as before.
  A library app's is `LIBRARY_APP_ID`: a source (a lowercase letter, then
  lowercase letters and digits), one dot, then a name in `INSTALL_ID`'s form,
  such as `openfit.frms`. `is_app_id()` takes either, and every check of an
  app's id uses it: uninstall, update, an app's presence (whether its data is
  orphaned), the app-sources routes and a Home layout item. `APP_ID` itself
  is unchanged, because it's also the rule for the keys of the metrics an app
  invents, and those never gain a dot.
- **The folder carries the source.** `install_library_app(apps_dir, source,
  files)` writes the library's files, as given, into `<source>.<id>/`
  (`openfit.frms/`); every other step is `install_app()`'s (the files
  checked, the id free or 409, `check_app()`, written whole or not at all).
  The scanner reads a folder named in `LIBRARY_APP_ID`'s shape as that app:
  its id is the folder's name, and its own `app.yaml` must say the bare name
  (`frms`), or nothing. Any other id there and it's skipped. The installed
  files stay byte-identical to the library's copies, so "is this install
  current?" is a plain file comparison for the Admin.
- **Only a library install makes a dot.** `INSTALL_ID` has none, so a pasted
  or duplicated app can never take a dotted id, and a library manifest that
  already carries one (`id: openfit.frms`) is refused. A dot in an id
  therefore always means installed from a library. A custom `frms` and the
  library's `openfit.frms` are two apps, side by side.
- **Read-only.** `update_app()` refuses a library app's id with a 403
  ("'openfit.frms' is from the openfit library, so it's updated there, not
  here — Duplicate makes a copy of your own to change"), nothing touched. Its
  row has no Update: `GET /api/apps` lists each app's `library` (`"openfit"`,
  or `null` for a custom app), and the page shows "From the openfit library ·
  updated by your admin" in Update's place. Uninstall stays, and its data
  stays, as any app's does.
- **Duplicate gives a copy of one's own.** The copy's id drops the source
  before it's numbered, so a copy of `openfit.frms` / "FRMS" is `frms-2` /
  "FRMS 2", installed through `install_app()` as a custom app.
- **A source is a source.** A library app writes under `app:openfit.frms`,
  its own metrics are `app:openfit.frms:<key>`, its sessions' ids
  `app:openfit.frms:<kind>:<start>`: the dotted id is just its id.

The owner's installed `frms` keeps running as it is: nothing moves its data
yet. That's `replaces`.

### Browse (B1, built)

The user system reads the shelf (`app/shelf.py`, at `/library`, or
`$OPENFIT_LIBRARY`) and installs from it, the page's Browse its front end
([app-host.md](app-host.md#browse-the-app-library)).

- **Reading the shelf.** The index's sources and their apps, by name; each
  source's folder must resolve inside the shelf, each app's inside its
  source's, and a file counts only if it's a regular file directly in the
  app's folder, opened without following a link. An app whose `app.yaml`
  doesn't read or check (its `name`, `description`, `requires`, `replaces`,
  and its id its folder's name) is left out and logged.
- **The list** (`GET /api/library`): every app, by name, with its
  description, files and a **state**: installed, unsupported (it has
  folders inside it), needs a newer OpenFit, replaces an app still here (it,
  or its data, is), else available. No shelf, or an index that doesn't read,
  and Browse says the library isn't connected yet: restart from the Admin.
- **The install** (`POST /api/library/<source>/<app>/install`): any body
  ignored, the state worked out afresh (409 with its words unless
  available), the files read from the shelf, then `install_library_app()`.
  The page shows the files read-only (`GET /api/library/<source>/<app>`)
  but never sends them, so what's installed is exactly what was published.
- **`replaces` is parsed and refused** (B2, which would move the data, is
  parked). FRMS declares `replaces: [frms]` (from the aviation repo,
  `openfitavi.frms`), so while the owner's `frms`, or its data, is here,
  the install refuses it, saying how to proceed (T1): "You have your own
  FRMS. To use this one instead, uninstall yours and delete its data under
  Your data first." - and Browse's row says the same: "You have your own
  FRMS. Uninstall it and delete its data under Your data to use this one."
- **An install of a library app already installed** is refused with "'<id>'
  is already installed. It updates itself when your admin publishes a new
  version." (T1): a library app has no Update, so a custom app's "use
  Update in its row" isn't said of one.
- **Each app's library by its name** (A6): every listed app carries
  `source_name`, its source's name from the shelf's index (OpenFit,
  OpenFit Aviation; else the source's id), which Browse's tag shows.

### Library apps follow the shelf (B3, built)

A library app is read-only in a user's OpenFit, so the shelf is the only
place a new version can come from - and every OpenFit that has it takes
it up by itself, with no restart. For an app developer that's the whole
flow: push to the app's repo, **Check now** in the Admin and the repo's
**Update available** (**Update apps** for the official library's), and
within a minute every installed copy is the new one
([app-host.md](app-host.md#library-apps-follow-the-shelf)).

- **Spotting a new version.** The index gives each app's fingerprint (the
  Admin's `fingerprint()`, its `sha256`), and an install is the shelf's
  files byte for byte, so the user system's own `fingerprint()` - the
  Admin's, reproduced exactly, a test holding the two to the byte -
  of the installed folder is the index's while it's current (the
  `__pycache__` a load leaves is skipped by both). One that differs is out
  of date.
- **When.** `sync_library_apps()`, at the user system's startup and then
  every 60 seconds (the `library-sync` scheduler job, on a worker of its
  own). No shelf mounted, nothing to do: no app is ever marked gone for it.
  A run with nothing to do reads the index and each installed library
  app's own files, to hash them.
- **Updating one.** Only a version this OpenFit can install - its
  `requires` within the app level, its own id its bare name, no folders
  inside it, `check_app()` - and that loads, staged, as the scanner would
  load it in place; then the same swap an Update makes (`_replace_app()`,
  which `update_app()` shares, keeping its 403 for a library app), its
  cached load dropped so the next render runs the new code. Its data,
  Home tiles and their config are never touched. A custom app never is.
- **The record** (`/data/app_updates.json`): each library app's latest
  event, `updated` (with the change notes the index had), `waiting` (it
  needs a newer OpenFit), `failed` (why) or `removed` (it, or its source,
  left the shelf - the copy stays, no longer updated). A version waiting or
  failed is never tried again: only another one is. `GET /api/apps` gives
  each library app its `library_note`; the Applications card shows an
  **Updated** pill for 14 days, opening What's new, or the note's sentence.
- **What changed** comes from the Admin: each app's index entry carries
  `changes`, the ten newest commits touching its folder, merges left out
  ([admin.md](admin.md#change-notes-b3)).

Out of B3: a user choosing when (the admin publishes, everyone follows),
updating custom apps from anywhere, and scheduled updates.

### `replaces` (L3, agreed; the move is B2, parked)

B1 parses `replaces` (a list of at most 5 app ids, none twice, never the
library app's own) and refuses the install while one is pending. B2 would
move the data, by these rules - parked, since the one app it was for, the
owner's `frms`, was removed and FRMS installed fresh (above): `replaces`
stays parsed and refused until a move is needed.

How a library app takes over an older app's id and data, such as the owner's
custom `frms` becoming `openfitavi.frms`:

- **Declared in the library copy's `app.yaml`**, naming the old id.
- **It runs only at install of the new app**, never on a scan or an update.
- **One transaction**, undone whole on any failure. It moves the old id's
  metric, session and sample sources; the session ids (`source:kind:start`);
  the keys of its custom metrics (`app:<old>:<key>`); the source roles that
  name it; and its Home layout items, config and all. Then it removes the old
  app's folder.
- **It adopts leftover data** from an old app that's already uninstalled.
- **It refuses rather than merges** when the new id already has data.
- **The user confirms first.**

## Users and access

### Accounts

- **One account per user container**, with a login page (U1, built:
  [auth.md](auth.md)). It's still not row-level: there's no `user_id`, and
  one person has one container and one database. A container is locked until
  its password is set. Two-factor sign-in (authenticator codes, which work
  offline) comes later. The Admin has its own, separate login.
- **The Admin creates users** through a one-time setup link, and the user
  sets their own password (U1, built: Setup link on the user's row, which
  runs the user system's own `auth_cli.py` in their container). The same
  link resets a forgotten password; there's no email.

### The backup file

- **One file, one format.** The OpenFit backup file is a zip holding the
  database, the apps folder, the device token stores and the secret key,
  plus the OpenFit version that made it. It's locked with a passphrase by
  default, with an option to leave out the device logins. Restoring into a
  newer version is fine, since the startup migrations upgrade it; restoring
  into an older version is refused. The same format serves a user's export,
  the backup taken before an update, and creating a user from a file.
- **A restore always creates a new user, with a new password.** The backup
  never carries a login. Built (R1, [admin.md](admin.md#restoring-a-backup-as-a-new-user-r1)),
  on the Admin's plain `tar.gz`: a file uploaded to Add user, or one of the
  server's own backups or a removed user's kept data, restored as a new
  user who always starts locked - the password and its sessions' key are
  never restored, whatever the file holds, and a setup link sets a new one.
  The device logins come along unless the Admin unticks Keep device logins
  (then they're left out, and the user re-adds each device). A backup from a
  newer OpenFit than the server runs is refused; an older one is migrated as
  it starts. This completes the backup file's restore rule: restoring always
  means a new password. Restoring over an existing user, or merging, is out.
- **A downloaded backup never carries a password** (S2, built). Whatever the
  Admin hands over - a backup, a removed user's kept data - leaves out the
  user's OpenFit password (`auth.json`) and its sessions' key
  (`.session_key`); restoring always means a new password through a setup
  link. That's the rule above, applied now to the Admin's plain `tar.gz`
  archives: the data, the apps and the device logins are in the file (and
  the page says so before the download), the login isn't. The backups the
  Admin keeps on the server, which Undo puts back, are untouched and still
  hold everything, so an Undo never locks anyone out.

### Reaching a container

_Deferred: the `.local` names, the proxy and the announcer below come after
the A-slices. For now each user system is reached at `<server IP>:<port>`,
as OpenFit is today on 8080, and the Admin at 8090._

- **On the home network**, a reverse proxy on port 80 routes `.local` names
  (`openfit-<user>.local`) to the user containers. An announcer on the host
  network, run by the Admin, publishes those names over mDNS; this was tested
  working from a PC on Debian 12 on Proxmox. The proxy and
  the announcer are containers of their own, so an Admin update never takes
  users offline. User containers publish no ports.
- **The Admin** is also reachable at `<server IP>:<port>`, as a fallback.
- **Away from home**, the route is Tailscale, never a third-party tunnel that
  sees the traffic.
- **Phones** are out of scope for now. A future app would find a server by
  the `_openfit._tcp` service it publishes.

## Build order

The Admin comes first now, in A-slices; the user system's login lands before
a second real person gets a container. Re-ordered 2026-10-27, after A1:

1. **A1: updates for the existing OpenFit** (built): check, update, undo, a
   backup before every update, behind a login of the Admin's own.
2. **A2: adding users** (built), one compose project per user, each on its
   own port, and updates and undo moving every user together.
3. **The user login (U1)** (built, [auth.md](auth.md)): the user system's
   login page, before any second real person gets a container (one account
   per container, still no `user_id`), locked until a password is set
   through a setup link the Admin makes.
4. **A4: the Admin's self-update** (built), through a short-lived helper
   container running the old Admin's image. Built ahead of the user login,
   so U1's Admin change rolls out with Update the Admin.
5. **Library serving and Browse**, with `replaces` (L3) and host-version
   labels (L4). The serving is built: **A5, the app shelf** (above), ahead of
   the user login. **Browse (B1)** is built: listing the shelf and installing
   from it, with `replaces` parsed and the app level (`requires`) checked.
   **B3** is built: installed library apps follow the shelf, updating
   themselves, with a note of what changed. B2, `replaces` moving an older
   app's data across (L3's rules), is parked (nothing to move); L4 is still
   to come. The library groundwork built before, L1 (corrected in B1) and
   L2, stands.
6. **The Admin Portal**: S1 (built), the page in OpenFit's style, with
   the reads behind its panels; S2 (built): reverting the Admin,
   downloading backups and kept data without the password, deleting single
   backups. Restoring a download is R1, below.
7. **Organisation repos** (A6, built): each a source of its own on the
   shelf, its name and blurb from its own `repo.yaml`, cloned over SSH with
   a read-only deploy key the Admin makes for it; FRMS moved to the first,
   `openfit-aviation`, and since B3 it updates itself from there.
8. **T1, a tidy-up** (built): five loose ends from the reviews of A6 to
   B3, none changing how anything is used. Browse's `replaces` refusal says
   how to proceed now B2 is parked (above); a library app already installed
   isn't told to "use Update in its row", which it hasn't got (above); the
   user system's session cookie is named by the port,
   `openfit_session_<port>`, so one browser stays signed in to two users'
   OpenFits on one server ([auth.md](auth.md#the-cookie) - rolling it out
   signs everyone out once); the Admin's swap helper stops the Admin before
   it puts any settings back - a rollback, a revert, a revert's fallback -
   so a slow new Admin can't write over them ([admin.md](admin.md)); and
   B3's guard on an app folder that leads out of the shelf is tested.
9. **R1: restore a backup as a new user** (built,
   [admin.md](admin.md#restoring-a-backup-as-a-new-user-r1)): Add user's
   Restore from a file, for a download (moving someone between servers),
   and Restore as a new user on a backup or kept data, with no download.
   The archive is read, never extracted by the Admin: every member checked
   (files and folders only, plain names, an archive bomb's limits, a sound
   `tracker.db` no newer than the server's OpenFit), then a clean copy
   written without the password, its sessions' key and the app update
   notes (and the device logins unless they're kept), which a helper
   extracts into the new user's brand new volume. They start locked: Setup
   link gives them a password.

### Parked

- **Scheduled updates**: an update at a time the admin picks, alongside "now"
  and the automatic check (which only ever finds an update, never applies
  one).
- **B2, `replaces` moving data**: the owner reinstalled FRMS fresh from
  Browse, so there's no old data to move. `replaces` stays parsed and
  refused until it's needed, then L3's rules (above) apply.

The proxy and the mDNS announcer are deferred: users are on `IP:port` until
then. The backup file's format (a zip, a passphrase, a user's own export) is
designed above and not yet placed in this order; the Admin's update backups
are plain `tar.gz` archives of each user's `/data` volume, not that format, a
download of one (S2) is the same archive less the password's two files, and
a restore (R1) takes either, always as a new user with a new password.
