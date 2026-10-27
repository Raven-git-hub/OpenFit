# OpenFit distribution: the library, the Admin and the user system

_Design record · decided 2026-10-27. Only the library folder is built (L1);
everything else here is designed, not yet in the code._

OpenFit is splitting into three layers: the repo, an **Admin** container that
manages user containers on a server, and the **user system** (today's `app/`).
Apps such as FRMS are no longer part of the user system. They live in a
**library** the Admin serves, and a user installs them from Browse. This
records how the three fit together, so each slice is built against one design
rather than its own guess. Until a slice lands, the code is as `CLAUDE.md`
describes it: no auth on core but the webhook's token, and no Admin.

## Layers

- **The repo.** One repo holds `app/` (the user system), `library/` (the
  official apps) and, later, `admin/`. The official repo is public. An
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

## Apps

- **The library** holds the official apps plus each organisation repo's. Any
  user can install any library app; there is no allowlist.
- **Names.** A library app is named `<source>.<name>`: `openfit.` for an
  official app, `openfitavi.` for the example aviation repo's. A `replaces`
  field lets a renamed or moved app carry its data across. That's a later
  slice, and two questions are open for it: who assigns a prefix, and how
  `replaces` moves the data. Until then an id is bare (`frms`) and can't hold
  a dot.
- **Host version.** A library app carries a host-version requirement, and
  the host refuses an install it isn't compatible with.
- **Library apps are the Admin's to update.** The Admin pushes a library
  app's updates. A user can't edit a library app (Duplicate gives them a
  custom copy to change), and sees an "updated, what changed" note when one
  is updated.
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

1. **Library groundwork:** this folder (L1, built), source-prefixed names
   with `replaces`, host-version labels, read-only library apps.
2. **The user system:** the login page, and the backup file (export,
   passphrase, restore).
3. **Browse**, reading a local library folder.
4. **The Admin:** adding a user (blank, or from a backup file), the library
   service, labelled containers, update checks and updates, and the separate
   proxy and announcer containers.
5. **The Admin's self-update helper**, and organisation repos.
