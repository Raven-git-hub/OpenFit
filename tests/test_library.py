"""
The official app library: library/, the apps OpenFit itself publishes -
none today. FRMS left it for the aviation repo (A6), its own
organisation repo, which the Admin publishes as openfitavi; the copy here
is a test fixture, tests/fixtures/apps/frms/, byte for byte what left.
The apps the host's tests run against as real apps (caffeine, import,
mood, steps, steps-metres, weight, workout-log) are fixtures too: they
were never official, and the shelf (A5) publishes whatever library/ holds
to every user.

library/ holds repo.yaml - the source's name and blurb - and an app per
folder, filed under its own id. Three things hold for the whole library,
whatever apps it gains: it sits outside the image's build context, so
it's never copied into the user system; every folder is named for the id
its app.yaml gives; and every app installs through the host's own install
path, from its files as text - what Browse does - so the library is
installable from the day an app is added to it.

Nothing here reads /data/apps; the installs go to a temp apps folder.
"""

from pathlib import Path

import pytest
import yaml

import admin_ops
import apps

ROOT = Path(__file__).parent.parent
LIBRARY = ROOT / "library"
FIXTURES = ROOT / "tests" / "fixtures" / "apps"
# admin_ops.fingerprint() of library/frms/ as it was before it left
# (3412b7d): the fixture is still exactly that.
FRMS_FINGERPRINT = "321bd9c7df6d3dc257ff0d1e5b708f8d9809d40d213c3adfff65d9c873ca84cd"


def library_apps():
    """Every app folder in the library, by name: none, today."""
    return sorted(p for p in LIBRARY.iterdir() if p.is_dir())


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
        # Its files, as Browse sends them: each plain file directly in the
        # folder (never a __pycache__ an import left), as text.
        files = {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir()) if p.is_file()}
        try:
            installed = apps.install_app(apps_dir, files)
        except apps.InstallError as e:
            pytest.fail(f"library/{folder.name} doesn't install: {e}")
        assert installed["id"] == folder.name


def test_the_official_library_is_repo_yaml_alone():
    assert sorted(p.name for p in LIBRARY.iterdir()) == ["repo.yaml"]
    repo = yaml.safe_load((LIBRARY / "repo.yaml").read_text(encoding="utf-8"))
    assert repo == {"name": "OpenFit", "description": "Official applications released by OpenFit."}


def test_frms_moved_to_the_fixtures_byte_for_byte_and_the_test_apps_are_fixtures_too():
    test_apps = ["caffeine", "frms", "import", "mood", "steps", "steps-metres", "weight", "workout-log"]
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) == test_apps
    for name in test_apps:
        assert apps.load_app(str(FIXTURES / name)).id == name
    assert admin_ops.fingerprint(str(FIXTURES / "frms")) == FRMS_FINGERPRINT
    assert sorted(p.name for p in (FIXTURES / "frms").iterdir() if p.is_file()) == [
        "app.py", "app.yaml", "config.json", "flash-dark.txt", "flash-light.txt"]
