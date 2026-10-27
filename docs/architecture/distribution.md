# OpenFit distribution: the library, the Admin and the user system

_Design record · decided 2026-10-27. Built so far: the library folder (L1),
library app ids with read-only library apps (L2), and the Admin's first slice,
updates for the existing OpenFit (A1, [admin.md](admin.md)); everything else
here is designed, not yet in the code._

OpenFit is splitting into three layers: the repo, an **Admin** container that
manages user containers on a server, and the **user system** (today's `app/`).
Apps such as FRMS are no longer part of the user system. They live in a
**library** the Admin serves, and a user installs them from Browse. This
records how the three fit together, so each slice is built against one design
rather than its own guess. Until a slice lands, the code is as `CLAUDE.md`
describes it: no auth on core but the webhook's token, and an Admin that
updates the one existing OpenFit (A1) and nothing more.

## Layers

- **The repo.** One repo holds `app/` (the user system), `library/` (the
  official apps) and `admin/` (the Admin, its first slice built). The official repo is public. An
  organisation's app repo is separate and private, and the Admin reads it with
  a read-only key.
- **The Admin.** One container per server. It creates users, checks for
  updates, applies them and serves the app library. It is the only OpenFit
  component that talks to the internet (GitHub), and only to check for
  updates or pull them.
- **The user system.** One container per user, with its own `/data` volume.
  It is fully offline apart from its own sync plugins' calls to their device
  APIs, as it is today.

## Updates

- **Checks** run automatically, weekly, or when the button is pressed. They
  are reported separately for the user system, the admin system and the app
  repository, each with its release notes. A change is classified by the
  folder it's in, so an app change never triggers an image rebuild: the
  library is outside the image (below).
- **Updating is always manual.** The user system image is built once per
  version, tagged by commit. Each user's data is backed up, then their
  container is recreated on the new image. The previous tag is kept, for
  rollback.
- **The Admin updates itself** through a short-lived helper container: stop
  the old Admin, start the new one, health-check it, roll back if it's
  unhealthy, exit.
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
by hand. The Admin's own update is still a command you run until A4. The full
record, with its safety rules (one compose project, the labelled container
and its own helpers only, no shell, git as the repo's owner), is
[admin.md](admin.md). Until the user containers are labelled by the Admin
itself (A2), "OpenFit's" means the compose labels of that one project.

## Apps

- **The library** holds the official apps plus each organisation repo's. Any
  user can install any library app; there is no allowlist.
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
  the host refuses an install it isn't compatible with.
- **Library apps are the Admin's to update.** The Admin pushes a library
  app's updates. A user can't edit a library app (Duplicate gives them a
  custom copy to change), and sees an "updated, what changed" note when one
  is updated. The read-only half is built (L2, below); the push and the note
  are the Admin's, to come.
- **A user's custom apps are theirs.** They never leave the user's
  container, and the Admin never sees them.

The library starts as `library/`, at the repo's top level: the apps OpenFit
publishes, one folder an app, named for its id, which the host's tests also run
against as real apps. It's outside the image's build context (the compose
file builds `./app`), so it's never copied into the user system: an app
reaches a user's `/data/apps` only by being installed. `tests/test_library.py`
holds the three things that make it a library: it sits outside the build
context, every folder is named for its app's id, and every app installs through
the host's own `install_app()` from its files as text, which is what Browse
will do. It's named `library/`, not `apps/`, so it can't be confused with
`app/`, and so it matches the Library tab whose Browse will read it.

### Library app ids (L2, built)

The host side of the names, the read-only rule and a function that installs
from a library. Nothing calls it from a route yet: Browse will.

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

### `replaces` (L3, agreed, not built)

How a library app takes over an older app's id and data, such as the owner's
custom `frms` becoming `openfit.frms`:

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

- **One account per user container**, with a login page. It's still not
  row-level: there's no `user_id`, and one person has one container and one
  database. Two-factor sign-in (authenticator codes, which work offline)
  comes later. The Admin has its own, separate login.
- **The Admin creates users** through a one-time setup link, and the user
  sets their own password.

### The backup file

- **One file, one format.** The OpenFit backup file is a zip holding the
  database, the apps folder, the device token stores and the secret key,
  plus the OpenFit version that made it. It's locked with a passphrase by
  default, with an option to leave out the device logins. Restoring into a
  newer version is fine, since the startup migrations upgrade it; restoring
  into an older version is refused. The same format serves a user's export,
  the backup taken before an update, and creating a user from a file.
- **A restore always creates a new user, with a new password.** The backup
  never carries a login.

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
2. **A2: adding users**, one compose project per user, each on its own port.
3. **The user login**: the user system's login page, before any second real
   person gets a container (one account per container, still no `user_id`).
4. **A4: the Admin's self-update**, through a short-lived helper container.
5. **Library serving and Browse**, with `replaces` (L3) and host-version
   labels (L4). L3 and L4 wait for Browse: nothing installs from the library
   until it exists. The library groundwork already built, L1 and L2, stands.
6. **Organisation repos.**

The proxy and the mDNS announcer are deferred: users are on `IP:port` until
then. The backup file (export, passphrase, restore) is designed above and not
yet placed in this order; A1's update backups are plain `tar.gz` archives of
the `/data` volume, not that format.
