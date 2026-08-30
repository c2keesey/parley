"""Credential-free tests for the native release mechanics."""

import importlib.util
import plistlib
import stat
import zipfile
from pathlib import Path

import pytest

from parley import __version__

SCRIPT = (
    Path(__file__).parents[1] / "macos" / "ParleyMenuBar" / "Scripts" / "release.py"
)
SPEC = importlib.util.spec_from_file_location("parley_native_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release)


def test_pyproject_is_the_single_release_version_source(tmp_path):
    assert release.project_version() == __version__

    destination = tmp_path / "Info.plist"
    release.render_plist(destination, __version__)
    with destination.open("rb") as handle:
        values = plistlib.load(handle)

    assert values["CFBundleShortVersionString"] == __version__
    assert values["CFBundleVersion"] == __version__


def test_version_mismatch_is_rejected():
    with pytest.raises(release.ReleaseError, match="version mismatch"):
        release.validate_version("0.5.3", "0.5.4")


def test_unsupported_architecture_is_rejected():
    with pytest.raises(release.ReleaseError, match="unsupported architecture"):
        release.validate_architectures(["arm64", "ppc64"])


def test_release_signing_mode_must_be_explicit():
    with pytest.raises(release.ReleaseError, match="PARLEY_SIGNING_IDENTITY"):
        release.validate_signing(ad_hoc=False, identity=None, notarize=False)
    with pytest.raises(release.ReleaseError, match="cannot be notarized"):
        release.validate_signing(ad_hoc=True, identity=None, notarize=True)


def test_raw_apple_id_password_is_rejected_without_reaching_argv_or_errors():
    secret = "super-secret-password"
    environment = {
        "PARLEY_NOTARY_APPLE_ID": "release@example.com",
        "PARLEY_NOTARY_TEAM_ID": "TEAMID",
        "PARLEY_NOTARY_PASSWORD": secret,
    }
    with pytest.raises(release.ReleaseError) as error:
        release.notarization_arguments(environment)

    assert secret not in str(error.value)


def test_raw_notary_password_never_reaches_child_argv_or_environment(
    monkeypatch,
):
    secret = "exact-secret-that-must-not-appear"
    monkeypatch.setenv("PARLEY_NOTARY_PASSWORD", secret)
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return release.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    release.run(["notarytool", "submit", "artifact.zip"])

    assert secret not in observed["command"]
    assert secret not in observed["environment"].values()
    assert "PARLEY_NOTARY_PASSWORD" not in observed["environment"]


def test_dirty_checkout_is_rejected(monkeypatch):
    monkeypatch.setattr(release, "output", lambda command: " M pyproject.toml")
    with pytest.raises(release.ReleaseError, match="clean git checkout"):
        release.ensure_clean_checkout()


def test_checksum_rejects_tampered_artifact_with_constant_time_compare(
    tmp_path, monkeypatch
):
    artifact = tmp_path / "ParleyMenuBar-0.5.4-macos-arm64-adhoc.zip"
    artifact.write_bytes(b"original")
    checksum = artifact.with_suffix(".zip.sha256")
    checksum.write_text(
        f"{release.sha256(artifact)}  {artifact.name}\n",
        encoding="utf-8",
    )
    artifact.write_bytes(b"tampered")
    comparisons = []

    def compare_digest(expected, actual):
        comparisons.append((expected, actual))
        return False

    monkeypatch.setattr(release.hmac, "compare_digest", compare_digest)

    with pytest.raises(release.ReleaseError, match="checksum mismatch"):
        release.verify_checksum(artifact, checksum)
    assert comparisons == [
        (
            checksum.read_text(encoding="utf-8").split()[0],
            release.sha256(artifact),
        )
    ]


def test_deterministic_zip_round_trip_restores_executable_mode(tmp_path):
    app = tmp_path / release.BUNDLE_NAME
    executable = app / "Contents" / "MacOS" / release.EXECUTABLE
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"native executable")
    executable.chmod(0o755)
    archive = tmp_path / "app.zip"
    second_archive = tmp_path / "app-second.zip"

    release.create_deterministic_zip(app, archive, 1_700_000_000)
    release.create_deterministic_zip(app, second_archive, 1_700_000_000)
    extracted = release.safe_extract(archive, tmp_path / "extracted")

    restored = extracted / "Contents" / "MacOS" / release.EXECUTABLE
    assert archive.read_bytes() == second_archive.read_bytes()
    assert restored.read_bytes() == b"native executable"
    assert stat.S_IMODE(restored.stat().st_mode) == 0o755


def test_unsafe_archive_entry_is_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape", "nope")

    with pytest.raises(release.ReleaseError, match="unsafe archive entry"):
        release.safe_extract(archive, tmp_path / "extract")


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_special_archive_entries_are_rejected(tmp_path, file_type):
    archive = tmp_path / f"special-{file_type}.zip"
    root = zipfile.ZipInfo(f"{release.BUNDLE_NAME}/")
    root.create_system = 3
    root.external_attr = (stat.S_IFDIR | 0o755) << 16
    special = zipfile.ZipInfo(f"{release.BUNDLE_NAME}/Contents/link")
    special.create_system = 3
    special.external_attr = (file_type | 0o755) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(root, b"")
        bundle.writestr(special, b"target")

    with pytest.raises(release.ReleaseError, match="unsupported archive entry type"):
        release.safe_extract(archive, tmp_path / "extract")
