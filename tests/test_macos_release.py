"""Credential-free tests for the native release mechanics."""

import importlib.util
import json
import plistlib
import stat
import sys
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

from parley import __version__

SCRIPT = (
    Path(__file__).parents[1] / "macos" / "ParleyMenuBar" / "Scripts" / "release.py"
)
SPEC = importlib.util.spec_from_file_location("parley_native_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def make_release_set(directory, **overrides):
    version = "0.5.4"
    architectures = ("arm64",)
    signature = "ad-hoc"
    stem = release.artifact_stem(version, architectures, signature)
    artifact = directory / f"{stem}.zip"
    artifact.write_bytes(b"signed app archive")
    digest = release.sha256(artifact)
    artifact.with_suffix(".zip.sha256").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8"
    )
    values = {
        "architectures": list(architectures),
        "artifact": artifact.name,
        "bundle_identifier": release.BUNDLE_IDENTIFIER,
        "bundle_version": version,
        "cli_distribution": f"parley-voice=={version}",
        "commit": "a" * 40,
        "contract_version": release.CONTRACT_VERSION,
        "notarized": False,
        "schema_version": release.MANIFEST_SCHEMA_VERSION,
        "sha256": digest,
        "signature": signature,
        "version": version,
    }
    values.update(overrides)
    manifest = directory / f"{stem}.json"
    manifest.write_text(json.dumps(values), encoding="utf-8")
    return manifest, artifact, values


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


@pytest.mark.parametrize(
    "version",
    [
        "0.5",
        "0.5.4.1",
        "0.5.4rc1",
        "v0.5.4",
        "01.2.3",
        "12345.1.1",
        "1.123.1",
        "1.1.123",
    ],
)
def test_bundle_version_requires_legal_numeric_three_component_form(version):
    with pytest.raises(release.ReleaseError, match="legal numeric CFBundleVersion"):
        release.validate_bundle_version(version)


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


def test_manifest_is_authoritative_and_agrees_with_checksum(tmp_path):
    manifest_path, artifact, values = make_release_set(tmp_path)

    manifest, resolved_artifact = release.verify_release_files(manifest_path)

    assert resolved_artifact == artifact
    assert manifest.version == values["version"]
    assert manifest.bundle_version == values["bundle_version"]
    assert manifest.architectures == tuple(values["architectures"])
    assert manifest.signature == values["signature"]
    assert manifest.sha256 == values["sha256"]


def test_verifier_derives_bundle_gates_only_from_manifest(tmp_path, monkeypatch):
    manifest_path, artifact, _ = make_release_set(tmp_path)
    manifest = release.read_release_manifest(manifest_path)
    observed = {}
    monkeypatch.setattr(
        release,
        "verify_release_files",
        lambda supplied: (manifest, artifact),
    )
    monkeypatch.setattr(release, "safe_extract", lambda archive, destination: tmp_path)
    monkeypatch.setattr(
        release,
        "verify_app",
        lambda app, **kwargs: observed.update(kwargs),
    )

    release.verify_release(manifest_path, smoke_launch=True)

    assert observed == {
        "version": manifest.version,
        "architectures": manifest.architectures,
        "signature": manifest.signature,
        "notarized": manifest.notarized,
        "gatekeeper": False,
        "smoke_launch": True,
    }


def test_manifest_sha_tamper_is_rejected_even_when_sidecar_matches(tmp_path):
    manifest_path, _, values = make_release_set(tmp_path, sha256="0" * 64)

    with pytest.raises(release.ReleaseError, match="manifest SHA-256"):
        release.verify_release_files(manifest_path)
    assert values["sha256"] == "0" * 64


def test_checksum_sidecar_tamper_is_rejected_even_when_manifest_matches(tmp_path):
    manifest_path, artifact, _ = make_release_set(tmp_path)
    artifact.with_suffix(".zip.sha256").write_text(
        f"{'0' * 64}  {artifact.name}\n", encoding="utf-8"
    )

    with pytest.raises(release.ReleaseError, match="checksum mismatch"):
        release.verify_release_files(manifest_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"contract_version": 2}, "contract version"),
        ({"bundle_identifier": "com.example.other"}, "bundle identifier"),
        ({"bundle_version": "0.5.3"}, "bundle version"),
        ({"cli_distribution": "parley-voice==0.5.3"}, "CLI distribution"),
        ({"architectures": ["x86_64"]}, "artifact name"),
        ({"signature": "developer-id"}, "artifact name"),
        ({"notarized": True}, "cannot claim notarization"),
        ({"artifact": "renamed.zip"}, "artifact name"),
        ({"schema_version": 2}, "schema version"),
    ],
)
def test_manifest_claim_mismatches_are_rejected(tmp_path, overrides, message):
    manifest_path, _, _ = make_release_set(tmp_path, **overrides)

    with pytest.raises(release.ReleaseError, match=message):
        release.read_release_manifest(manifest_path)


def stub_release_build(monkeypatch, *, failure):
    monkeypatch.setattr(release, "project_version", lambda: "0.5.4")
    monkeypatch.setattr(release, "ensure_clean_checkout", lambda: "a" * 40)
    monkeypatch.setattr(release, "source_date_epoch", lambda: 1_700_000_000)
    monkeypatch.setattr(release, "notarization_arguments", lambda: [])
    monkeypatch.setattr(release, "assemble_app", lambda stage, version, arches: stage)
    monkeypatch.setattr(release, "sign_app", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "verify_app", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        release,
        "create_deterministic_zip",
        lambda app, archive, epoch: archive.write_bytes(b"staged archive"),
    )
    if failure == "notary":
        monkeypatch.setattr(
            release,
            "run",
            lambda command, **kwargs: (_ for _ in ()).throw(
                release.ReleaseError("notarization failed")
            ),
        )
    elif failure == "verify":
        monkeypatch.setattr(
            release,
            "verify_release",
            lambda manifest: (_ for _ in ()).throw(
                release.ReleaseError("final verification failed")
            ),
        )


@pytest.mark.parametrize("failure", ["notary", "verify"])
def test_failed_build_leaves_no_plausible_release_residue(
    tmp_path, monkeypatch, failure
):
    stub_release_build(monkeypatch, failure=failure)
    output_dir = tmp_path / "out"
    args = Namespace(
        version="0.5.4",
        architectures=["arm64"],
        ad_hoc=failure != "notary",
        signing_identity=(
            None if failure != "notary" else "Developer ID Application: Test"
        ),
        notarize=failure == "notary",
        output_dir=output_dir,
    )

    with pytest.raises(release.ReleaseError, match="failed"):
        release.build_release(args)

    assert output_dir.is_dir()
    assert list(output_dir.iterdir()) == []


def test_existing_release_output_is_refused_before_build(tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    stem = "ParleyMenuBar-0.5.4-macos-arm64-adhoc"
    existing = output_dir / stem
    existing.mkdir()
    marker = existing / "keep"
    marker.write_text("original", encoding="utf-8")
    monkeypatch.setattr(release, "project_version", lambda: "0.5.4")
    monkeypatch.setattr(release, "ensure_clean_checkout", lambda: "a" * 40)
    monkeypatch.setattr(release, "source_date_epoch", lambda: 1_700_000_000)
    monkeypatch.setattr(
        release,
        "assemble_app",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not build")),
    )
    args = Namespace(
        version="0.5.4",
        architectures=["arm64"],
        ad_hoc=True,
        signing_identity=None,
        notarize=False,
        output_dir=output_dir,
    )

    with pytest.raises(release.ReleaseError, match="refusing to overwrite"):
        release.build_release(args)

    assert marker.read_text(encoding="utf-8") == "original"


def test_complete_release_set_is_published_as_one_directory(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    staged = output_dir / ".private-stage"
    staged.mkdir()
    expected_names = {"artifact.zip", "artifact.zip.sha256", "artifact.json"}
    for name in expected_names:
        (staged / name).write_text(name, encoding="utf-8")

    published = release.publish_release_set(staged, output_dir, "release-set")

    assert published == output_dir / "release-set"
    assert {item.name for item in published.iterdir()} == expected_names
    assert not staged.exists()


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
