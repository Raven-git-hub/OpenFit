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

from abc import ABC, abstractmethod


class SyncPlugin(ABC):
    # Short machine id, used in URLs and the activity.source column.
    # e.g. "garmin", "google_health", "oura"
    id: str

    # Human-readable name shown in the UI.
    name: str

    # Env var names this plugin needs to function. Used only to give a
    # clear error message if they're missing - main.py doesn't validate
    # these itself, the plugin does in sync().
    required_env: list[str] = []

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

    def status(self) -> dict:
        """
        Optional: cheap, non-network check of whether this plugin looks
        configured (e.g. env vars present). Used for a status indicator
        in the UI. Default just checks required_env is set.
        """
        import os
        missing = [v for v in self.required_env if not os.getenv(v)]
        return {"configured": not missing, "missing_env": missing}
