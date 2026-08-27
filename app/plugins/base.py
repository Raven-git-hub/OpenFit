"""
OpenFit plugin interface.

A "sync plugin" is anything that pulls data from an external device/service
and writes it into OpenFit's activity table. To add a new source (Google
Health, Oura, Whoop, a spreadsheet, whatever), subclass SyncPlugin and drop
the module in app/plugins/<your_plugin>/.

Plugins are intentionally dumb: they get a database connection and a date
range, and they write rows. All the scheduling, API routes, and UI wiring
already exist in main.py and don't need to change per-plugin.
"""

import json
import os
from abc import ABC, abstractmethod

from crypto import decrypt

# Where a provider sends the browser back after consent, for every
# add_flow="oauth" plugin.
#
# It is a loopback address this app deliberately does NOT serve: the
# browser lands on a "can't connect" page with ?code=... sitting in the
# address bar, which is exactly what the paste-the-code flow needs. That
# keeps OpenFit self-hosted - no hosted redirect, no domain, no callback
# route to secure - and desktop-app OAuth clients accept any loopback
# port without registering it.
OAUTH_REDIRECT_URI = "http://127.0.0.1:9109/"


class SyncPlugin(ABC):
    # Short machine id, used in URLs and the activity.source column.
    # e.g. "garmin", "google_health", "oura"
    id: str

    # Human-readable name shown in the UI.
    name: str

    # ---- connector manifest ----
    #
    # `fields` describes what this plugin needs to authenticate, and is
    # what the UI renders as a form when you add the device. Each entry:
    #
    #   key      - name used in the stored credentials JSON
    #   label    - shown to the human
    #   type     - "text" or "password" (drives the input type)
    #   required - whether the API rejects an add without it
    #   env      - legacy env var holding this value, if there is one.
    #              Only used for the fallback in credentials_from_env();
    #              new plugins can leave it out entirely.
    #
    # Declaring a field is the whole integration - main.py builds the
    # form, the validation and the storage from this list.
    fields: list[dict] = []

    # How the device is added: "credentials" (fill in the fields above)
    # or "oauth" (fill in the fields above, then bounce through a
    # provider consent screen - see the oauth_* methods below). The UI
    # renders a one-step form for the first and a two-step one for the
    # second, from this value alone.
    add_flow: str = "credentials"

    # Optional guidance shown above the form when adding this device -
    # e.g. what to set up at the provider before an OAuth client will
    # work. Plain text; the UI escapes it. Declared here so setup notes
    # stay with the plugin instead of hardcoded in the template.
    add_note: str = ""

    # Shown when an add_flow="oauth" provider completes the exchange but
    # hands back no refresh token - the one failure whose fix is always
    # provider-specific ("revoke access over there, then retry"). Left
    # blank, main.py falls back to a generic version of the same advice.
    oauth_refresh_help: str = ""

    # Env var names this plugin needs to function. Superseded by `fields`
    # for anything with a manifest; kept for plugins that have none.
    required_env: list[str] = []

    # ---- credentials ----

    def credentials_from_env(self) -> dict:
        """Manifest values sourced from the legacy environment variables.

        This is what keeps a pre-UI install working: someone whose Garmin
        details are in .env keeps syncing after upgrading, without having
        re-added the device through the UI yet.
        """
        out = {}
        for field in self.fields:
            env = field.get("env")
            value = os.getenv(env) if env else None
            if value:
                out[field["key"]] = value
        return out

    def get_credentials(self, conn=None) -> dict:
        """This plugin's credentials: the stored account, else the env.

        Reads the `accounts` row, decrypts the JSON blob and returns it as
        a dict. With no row (or no connection), falls back to the env
        vars named in the manifest.
        """
        if conn is not None:
            row = conn.execute(
                "SELECT credentials FROM accounts WHERE plugin_id = ?", (self.id,)
            ).fetchone()
            if row and row[0]:
                try:
                    stored = json.loads(decrypt(row[0]))
                except Exception as e:
                    # A lost or rotated /data/.secret_key makes every
                    # stored credential undecryptable. That must not 500
                    # the connectors API - the UI is where you'd remove
                    # and re-add the device to fix it.
                    print(
                        f"[{self.id}] stored credentials could not be read ({e}) - "
                        "remove and re-add this device"
                    )
                    stored = None
                if isinstance(stored, dict):
                    # An env var still fills a gap the stored account
                    # doesn't cover - e.g. a field added to the manifest
                    # after the device was connected.
                    return {**self.credentials_from_env(), **stored}
        return self.credentials_from_env()

    def missing_fields(self, credentials: dict) -> list[dict]:
        """Required manifest fields with no value in `credentials`."""
        return [
            f for f in self.fields
            if f.get("required") and not credentials.get(f["key"])
        ]

    # ---- oauth (add_flow == "oauth" only) ----
    #
    # Two methods are the whole flow. main.py owns the PKCE pair, the
    # pending-state store and the encrypted storage; the plugin owns
    # nothing but its provider's URLs, scopes and parameter names. A new
    # OAuth device is these two methods plus a manifest - no route and
    # no frontend code.

    def oauth_auth_url(self, credentials: dict, redirect_uri: str, code_challenge: str) -> str:
        """The provider consent URL to send the human to.

        `credentials` holds the manifest fields just entered (client id,
        secret, whatever this provider needs). `code_challenge` is the
        S256 challenge for the PKCE verifier main.py is holding; put it
        in the URL so the token exchange can prove it owns the code.
        Ask for offline access - a refresh token is the point.
        """
        raise NotImplementedError(f"{self.id} does not implement oauth_auth_url")

    def oauth_exchange(self, credentials: dict, code: str, code_verifier: str,
                       redirect_uri: str) -> dict:
        """Trade the authorization code for the credentials to store.

        Returns the full dict that gets encrypted into the account row -
        typically the manifest fields plus the refresh token. It MUST
        contain a refresh_token: without one the connection dies the
        moment the first access token expires, so main.py rejects the
        add rather than storing something that will quietly stop
        working. Raise on a failed exchange.
        """
        raise NotImplementedError(f"{self.id} does not implement oauth_exchange")

    def clear_cached_auth(self) -> None:
        """Drop any token/session this plugin cached on disk.

        Called when the device is removed, so disconnecting is a real
        disconnect and not just a forgotten password. Default is a no-op
        for plugins that cache nothing.
        """
        return None

    @abstractmethod
    def sync(self, conn, days: int) -> int:
        """
        Pull the last `days` days of data from the source and upsert into
        the `activity` table on the given sqlite3 connection.

        The activity table is keyed on (date, source), so each plugin owns
        its own rows and two sources covering the same day never contend.
        Write your own `id` into the source column and use
        INSERT ... ON CONFLICT(date, source) DO UPDATE, COALESCE-ing
        against the existing columns so a later partial sync doesn't blank
        out fields an earlier one filled in. See
        plugins/garmin/plugin.py for the reference implementation.

        Returns the number of days written (for logging/UI feedback).
        Should raise on hard failure (bad credentials, network error) -
        main.py catches this and reports it, but does not need each
        plugin to fail silently.
        """
        raise NotImplementedError

    def status(self, conn=None) -> dict:
        """
        Cheap, non-network check of whether this plugin looks configured.
        Used for the status indicator in the UI.

        With a manifest, "configured" means every required field has a
        value - from the stored account if the device was added in the
        UI, otherwise from the environment. Without one, it falls back to
        the old required_env check.
        """
        if not self.fields:
            missing = [v for v in self.required_env if not os.getenv(v)]
            return {"configured": not missing, "missing_env": missing}

        missing = self.missing_fields(self.get_credentials(conn))
        return {
            "configured": not missing,
            # Still called missing_env for the UI's sake; for a manifest
            # plugin it names the env var where one exists, else the field.
            "missing_env": [f.get("env") or f["key"] for f in missing],
        }
