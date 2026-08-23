"""
Plugin registry. main.py imports PLUGINS from here.

To add a plugin: write it in its own subfolder implementing SyncPlugin
(see base.py), then add one line below. That's the whole integration
point - no changes needed elsewhere for the plugin to show up in the API
and UI.
"""

from .garmin.plugin import GarminPlugin
from .google_health.plugin import GoogleHealthPlugin

PLUGINS = {
    p.id: p
    for p in [
        GarminPlugin(),
        GoogleHealthPlugin(),
    ]
}
