"""
The official app library: library/, the apps OpenFit publishes.

Each folder in it is one app, filed under its own id, and the host's
tests run against them as real apps. Three things hold for the whole
library, whatever apps it gains: it sits outside the image's build
context, so it's never copied into the user system; every folder is
named for the id its app.yaml gives; and every app installs through the
host's own install path, from its files as text - what Browse will do -
so the library is installable from the day an app is added to it.

Nothing here reads /data/apps; the installs go to a temp apps folder.
"""

from pathlib import Path

import pytest
import yaml

import apps

ROOT = Path(__file__).parent.parent
LIBRARY = ROOT / "library"


def library_apps():
    """Every app folder in the library, by name."""
    folders = sorted(p for p in LIBRARY.iterdir() if p.is_dir())
    assert folders, "the library has no apps"
    return folders


def test_the_library_sits_outside_the_image_build_context():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    build = compose["services"]["tracker"]["build"]
    # The short form is the context's path; the long form names it.
    context = (ROOT / (build["context"] if isinstance(build, dict) else build)).resolve()

    assert context.is_dir()
    assert not LIBRARY.resolve().is_relative_to(context)


def test_every_library_app_is_filed_under_its_own_id():
    for folder in library_apps():
        assert apps.load_app(folder).id == folder.name


def test_every_library_app_installs_through_the_hosts_own_install_path(tmp_path):
    # All into one apps folder, so two library apps under one id would
    # clash here (409) as they would on a user's volume.
    apps_dir = tmp_path / "apps"
    for folder in library_apps():
        # Its files, as Browse will send them: each plain file directly in
        # the folder (never a __pycache__ an import left), as text.
        files = {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir()) if p.is_file()}
        try:
            installed = apps.install_app(apps_dir, files)
        except apps.InstallError as e:
            pytest.fail(f"library/{folder.name} doesn't install: {e}")
        assert installed["id"] == folder.name
