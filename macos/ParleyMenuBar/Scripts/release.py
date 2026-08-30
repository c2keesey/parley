#!/usr/bin/env python3
"""Build and verify normalized ParleyMenuBar release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = Path(__file__).resolve().parents[3]
PLIST_TEMPLATE = PACKAGE_DIR / "Resources" / "Info.plist"
EXECUTABLE = "ParleyMenuBar"
BUNDLE_NAME = f"{EXECUTABLE}.app"
BUNDLE_IDENTIFIER = "com.chriskeesey.parley.menubar"
CONTRACT_VERSION = 1
SUPPORTED_ARCHITECTURES = ("arm64", "x86_64")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class ReleaseError(RuntimeError):
    """A safe, user-facing release validation failure."""


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Raw Apple-ID passwords are not an accepted input and must not leak into a
    # child process merely because one happens to exist in the caller's shell.
    environment.pop("PARLEY_NOTARY_PASSWORD", None)
    return environment


def run(
    command: list[str], *, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a command without logging its arguments (which can contain credentials)."""
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=capture,
            env=child_environment(),
        )
    except FileNotFoundError as error:
        raise ReleaseError(f"required tool not found: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        suffix = f": {detail.splitlines()[-1]}" if detail else ""
        raise ReleaseError(
            f"command failed ({Path(command[0]).name}){suffix}"
        ) from error


def output(command: list[str]) -> str:
    return run(command, capture=True).stdout.strip()


def project_version(repo_dir: Path = REPO_DIR) -> str:
    import tomllib

    with (repo_dir / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ReleaseError(f"unsupported project version: {version!r}")
    return version


def validate_version(expected: str, actual: str) -> None:
    if expected != actual:
        raise ReleaseError(
            f"version mismatch: requested {expected}, pyproject.toml declares {actual}"
        )


def validate_architectures(architectures: list[str]) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(architectures))
    if not values:
        raise ReleaseError("at least one architecture is required")
    unsupported = sorted(set(values) - set(SUPPORTED_ARCHITECTURES))
    if unsupported:
        raise ReleaseError(f"unsupported architecture(s): {', '.join(unsupported)}")
    return tuple(
        architecture
        for architecture in SUPPORTED_ARCHITECTURES
        if architecture in values
    )


def ensure_clean_checkout(repo_dir: Path = REPO_DIR) -> str:
    status = output(
        [
            "git",
            "-C",
            str(repo_dir),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ]
    )
    if status:
        raise ReleaseError("release builds require a clean git checkout")
    return output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"])


def source_date_epoch(repo_dir: Path = REPO_DIR) -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        raw = output(["git", "-C", str(repo_dir), "show", "-s", "--format=%ct", "HEAD"])
    try:
        epoch = int(raw)
    except ValueError as error:
        raise ReleaseError(
            "SOURCE_DATE_EPOCH must be an integer Unix timestamp"
        ) from error
    if epoch < 315532800:
        raise ReleaseError("SOURCE_DATE_EPOCH must be on or after 1980-01-01")
    return epoch


def validate_signing(*, ad_hoc: bool, identity: str | None, notarize: bool) -> str:
    if ad_hoc and identity:
        raise ReleaseError("choose either --ad-hoc or a Developer ID signing identity")
    if not ad_hoc and not identity:
        raise ReleaseError(
            "set PARLEY_SIGNING_IDENTITY/--signing-identity or explicitly use --ad-hoc"
        )
    if notarize and ad_hoc:
        raise ReleaseError("ad-hoc artifacts cannot be notarized")
    return "ad-hoc" if ad_hoc else "developer-id"


def notarization_arguments(environment: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environment is None else environment
    profile = env.get("PARLEY_NOTARY_KEYCHAIN_PROFILE")
    if profile:
        arguments = ["--keychain-profile", profile]
        if keychain := env.get("PARLEY_NOTARY_KEYCHAIN"):
            arguments.extend(["--keychain", keychain])
        return arguments

    api_values = [
        env.get("PARLEY_NOTARY_KEY"),
        env.get("PARLEY_NOTARY_KEY_ID"),
        env.get("PARLEY_NOTARY_ISSUER_ID"),
    ]
    if any(api_values):
        if not all(api_values):
            raise ReleaseError(
                "API-key notarization requires PARLEY_NOTARY_KEY, "
                "PARLEY_NOTARY_KEY_ID, and PARLEY_NOTARY_ISSUER_ID"
            )
        return [
            "--key",
            api_values[0],
            "--key-id",
            api_values[1],
            "--issuer",
            api_values[2],
        ]

    raise ReleaseError(
        "notarization credentials are missing; configure a keychain profile "
        "or App Store Connect API key inputs"
    )


def render_plist(destination: Path, version: str) -> None:
    with PLIST_TEMPLATE.open("rb") as handle:
        values = plistlib.load(handle)
    if values.get("CFBundleShortVersionString") != "@PARLEY_VERSION@":
        raise ReleaseError("Info.plist marketing version must use @PARLEY_VERSION@")
    if values.get("CFBundleVersion") != "@PARLEY_VERSION@":
        raise ReleaseError("Info.plist build version must use @PARLEY_VERSION@")
    values["CFBundleShortVersionString"] = version
    values["CFBundleVersion"] = version
    with destination.open("wb") as handle:
        plistlib.dump(values, handle, sort_keys=True)


def swift_binary(architecture: str, scratch_dir: Path) -> Path:
    triple = f"{architecture}-apple-macosx13.0"
    base = [
        "swift",
        "build",
        "--package-path",
        str(PACKAGE_DIR),
        "--scratch-path",
        str(scratch_dir),
        "--configuration",
        "release",
        "--triple",
        triple,
        "--product",
        EXECUTABLE,
    ]
    print(f"Building {EXECUTABLE} for {architecture}…")
    run(base)
    binary_dir = Path(output(base + ["--show-bin-path"]))
    binary = binary_dir / EXECUTABLE
    if not binary.is_file():
        raise ReleaseError(f"Swift build did not produce {binary}")
    return binary


def assemble_app(stage_dir: Path, version: str, architectures: tuple[str, ...]) -> Path:
    app = stage_dir / BUNDLE_NAME
    contents = app / "Contents"
    executable_dir = contents / "MacOS"
    resources_dir = contents / "Resources"
    executable_dir.mkdir(parents=True)
    resources_dir.mkdir()
    binaries = [
        swift_binary(architecture, stage_dir / f"swift-{architecture}")
        for architecture in architectures
    ]
    destination = executable_dir / EXECUTABLE
    if len(binaries) == 1:
        shutil.copyfile(binaries[0], destination)
    else:
        run(
            [
                "xcrun",
                "lipo",
                "-create",
                *map(str, binaries),
                "-output",
                str(destination),
            ]
        )
    destination.chmod(0o755)
    render_plist(contents / "Info.plist", version)
    return app


def sign_app(app: Path, *, ad_hoc: bool, identity: str | None) -> None:
    if ad_hoc:
        run(
            [
                "codesign",
                "--force",
                "--options",
                "runtime",
                "--sign",
                "-",
                "--timestamp=none",
                str(app),
            ]
        )
    else:
        run(
            [
                "codesign",
                "--force",
                "--options",
                "runtime",
                "--sign",
                identity or "",
                "--timestamp",
                str(app),
            ]
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(artifact: Path, checksum_file: Path) -> str:
    fields = checksum_file.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1] != artifact.name:
        raise ReleaseError("checksum file must contain '<sha256>  <artifact filename>'")
    actual = sha256(artifact)
    if not hmac.compare_digest(fields[0].lower(), actual):
        raise ReleaseError(f"checksum mismatch for {artifact.name}")
    return actual


def zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    value = time.gmtime(epoch)
    return (
        value.tm_year,
        value.tm_mon,
        value.tm_mday,
        value.tm_hour,
        value.tm_min,
        value.tm_sec - value.tm_sec % 2,
    )


def create_deterministic_zip(app: Path, archive: Path, epoch: int) -> None:
    timestamp = zip_timestamp(epoch)
    entries = [
        app,
        *sorted(
            app.rglob("*"), key=lambda item: item.relative_to(app.parent).as_posix()
        ),
    ]
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as bundle:
        for item in entries:
            relative = item.relative_to(app.parent).as_posix()
            is_directory = item.is_dir()
            name = f"{relative}/" if is_directory else relative
            info = zipfile.ZipInfo(name, timestamp)
            info.create_system = 3
            mode = (0o40755 if is_directory else item.stat().st_mode) & 0xFFFF
            info.external_attr = mode << 16
            info.compress_type = (
                zipfile.ZIP_STORED if is_directory else zipfile.ZIP_DEFLATED
            )
            data = b"" if is_directory else item.read_bytes()
            bundle.writestr(
                info, data, compress_type=info.compress_type, compresslevel=9
            )


def safe_extract(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        validated: list[tuple[zipfile.ZipInfo, PurePosixPath, int, bool]] = []
        entry_types: dict[PurePosixPath, bool] = {}
        for info in bundle.infolist():
            relative = PurePosixPath(info.filename)
            if info.filename.startswith("/") or ".." in relative.parts:
                raise ReleaseError(f"unsafe archive entry: {info.filename}")
            if not relative.parts or relative in entry_types:
                raise ReleaseError(f"duplicate or empty archive entry: {info.filename}")
            if relative.parts[0] != BUNDLE_NAME:
                raise ReleaseError(
                    f"unexpected top-level archive entry: {info.filename}"
                )
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            is_directory = info.is_dir()
            expected_kind = stat.S_IFDIR if is_directory else stat.S_IFREG
            if kind != expected_kind:
                raise ReleaseError(f"unsupported archive entry type: {info.filename}")
            if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                raise ReleaseError(f"unsafe archive entry mode: {info.filename}")
            entry_types[relative] = is_directory
            validated.append((info, relative, stat.S_IMODE(mode), is_directory))

        root = PurePosixPath(BUNDLE_NAME)
        if entry_types.get(root) is not True:
            raise ReleaseError(
                f"archive must contain a top-level {BUNDLE_NAME} directory"
            )
        for relative in entry_types:
            for parent in relative.parents:
                if parent == PurePosixPath("."):
                    break
                if parent in entry_types and not entry_types[parent]:
                    raise ReleaseError(f"archive parent is not a directory: {parent}")

        destination.mkdir(parents=True, exist_ok=True)
        for info, relative, permissions, is_directory in validated:
            target = destination.joinpath(*relative.parts)
            if is_directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("xb") as output_file:
                    shutil.copyfileobj(source, output_file)
            target.chmod(permissions)
    return destination / BUNDLE_NAME


def inspect_signature(app: Path, signature: str) -> None:
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    details = run(["codesign", "--display", "--verbose=4", str(app)], capture=True)
    signature_details = f"{details.stdout}\n{details.stderr}"
    if signature == "ad-hoc" and "Signature=adhoc" not in signature_details:
        raise ReleaseError("expected an ad-hoc code signature")
    if (
        signature == "developer-id"
        and "Authority=Developer ID Application:" not in signature_details
    ):
        raise ReleaseError("expected a Developer ID Application signature")


def verify_app(
    app: Path,
    *,
    version: str,
    architectures: tuple[str, ...],
    signature: str,
    notarized: bool = False,
    gatekeeper: bool = False,
    smoke_launch: bool = False,
) -> None:
    if not app.is_dir():
        raise ReleaseError(f"app bundle not found: {app}")
    plist_path = app / "Contents" / "Info.plist"
    executable = app / "Contents" / "MacOS" / EXECUTABLE
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ReleaseError(
            f"bundle executable is missing or not executable: {executable}"
        )
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    expected = {
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "13.0",
    }
    mismatches = [
        f"{key}={plist.get(key)!r}"
        for key, value in expected.items()
        if plist.get(key) != value
    ]
    if mismatches:
        raise ReleaseError(f"bundle plist mismatch: {', '.join(mismatches)}")
    actual_architectures = set(
        output(["xcrun", "lipo", "-archs", str(executable)]).split()
    )
    if actual_architectures != set(architectures):
        raise ReleaseError(
            "bundle architecture mismatch: expected "
            f"{','.join(architectures)}, found {','.join(sorted(actual_architectures))}"
        )
    inspect_signature(app, signature)
    if notarized:
        run(["xcrun", "stapler", "validate", str(app)])
    if gatekeeper:
        if signature != "developer-id":
            raise ReleaseError("Gatekeeper assessment requires a Developer ID artifact")
        run(["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)])
    if smoke_launch:
        process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_environment(),
        )
        try:
            time.sleep(2)
            if process.poll() is not None:
                raise ReleaseError(
                    f"direct native launch exited with status {process.returncode}"
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def architecture_label(architectures: tuple[str, ...]) -> str:
    return (
        "universal2" if architectures == SUPPORTED_ARCHITECTURES else architectures[0]
    )


def artifact_stem(version: str, architectures: tuple[str, ...], signature: str) -> str:
    suffix = "-adhoc" if signature == "ad-hoc" else ""
    return f"ParleyMenuBar-{version}-macos-{architecture_label(architectures)}{suffix}"


def write_release_metadata(
    output_dir: Path,
    *,
    archive: Path,
    version: str,
    architectures: tuple[str, ...],
    signature: str,
    commit: str,
) -> tuple[Path, Path]:
    digest = sha256(archive)
    checksum = archive.with_suffix(f"{archive.suffix}.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    manifest = output_dir / f"{archive.stem}.json"
    values = {
        "architectures": list(architectures),
        "artifact": archive.name,
        "bundle_identifier": BUNDLE_IDENTIFIER,
        "cli_distribution": f"parley-voice=={version}",
        "commit": commit,
        "contract_version": CONTRACT_VERSION,
        "schema_version": 1,
        "sha256": digest,
        "signature": signature,
        "version": version,
    }
    manifest.write_text(
        json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return checksum, manifest


def build_release(args: argparse.Namespace) -> None:
    actual_version = project_version()
    validate_version(args.version, actual_version)
    architectures = validate_architectures(args.architectures)
    signature = validate_signing(
        ad_hoc=args.ad_hoc, identity=args.signing_identity, notarize=args.notarize
    )
    notary_arguments = notarization_arguments() if args.notarize else []
    commit = ensure_clean_checkout()
    epoch = source_date_epoch()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = artifact_stem(actual_version, architectures, signature)
    archive = output_dir / f"{stem}.zip"
    with tempfile.TemporaryDirectory(
        prefix=".parley-release-", dir=output_dir
    ) as temporary:
        stage = Path(temporary)
        app = assemble_app(stage, actual_version, architectures)
        sign_app(app, ad_hoc=args.ad_hoc, identity=args.signing_identity)
        verify_app(
            app,
            version=actual_version,
            architectures=architectures,
            signature=signature,
        )
        create_deterministic_zip(app, archive, epoch)
        if args.notarize:
            print("Submitting signed archive for notarization…")
            run(
                [
                    "xcrun",
                    "notarytool",
                    "submit",
                    str(archive),
                    "--wait",
                    *notary_arguments,
                ]
            )
            run(["xcrun", "stapler", "staple", str(app)])
            verify_app(
                app,
                version=actual_version,
                architectures=architectures,
                signature=signature,
                notarized=True,
                gatekeeper=True,
            )
            create_deterministic_zip(app, archive, epoch)
    checksum, manifest = write_release_metadata(
        output_dir,
        archive=archive,
        version=actual_version,
        architectures=architectures,
        signature=signature,
        commit=commit,
    )
    print(archive)
    print(checksum)
    print(manifest)


def verify_artifact(args: argparse.Namespace) -> None:
    architectures = validate_architectures(args.architectures)
    verify_checksum(args.artifact, args.checksum)
    with tempfile.TemporaryDirectory(prefix="parley-verify-") as temporary:
        app = safe_extract(args.artifact, Path(temporary))
        verify_app(
            app,
            version=args.version,
            architectures=architectures,
            signature=args.signature,
            notarized=args.notarized,
            gatekeeper=args.gatekeeper,
            smoke_launch=args.smoke_launch,
        )
    print(f"Verified {args.artifact.name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    version = subparsers.add_parser(
        "version", help="print the canonical project version"
    )
    version.add_argument("--expect", help="fail unless the canonical version matches")

    build = subparsers.add_parser("build", help="build a signed release artifact")
    build.add_argument(
        "--version", required=True, help="expected pyproject.toml version"
    )
    build.add_argument(
        "--architectures",
        nargs="+",
        default=list(SUPPORTED_ARCHITECTURES),
        help="macOS architectures (default: arm64 x86_64)",
    )
    signing = build.add_mutually_exclusive_group()
    signing.add_argument(
        "--ad-hoc", action="store_true", help="build a local-only validation artifact"
    )
    signing.add_argument(
        "--signing-identity",
        default=os.environ.get("PARLEY_SIGNING_IDENTITY"),
        help="Developer ID Application identity (or PARLEY_SIGNING_IDENTITY)",
    )
    build.add_argument(
        "--notarize",
        action="store_true",
        help="submit, staple, and assess with Gatekeeper",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_DIR / ".artifacts" / "release",
    )
    build.set_defaults(handler=build_release)

    app = subparsers.add_parser("verify-app", help="verify an assembled app bundle")
    app.add_argument("--app", required=True, type=Path)
    app.add_argument("--version", required=True)
    app.add_argument("--architectures", nargs="+", required=True)
    app.add_argument("--signature", choices=("ad-hoc", "developer-id"), required=True)
    app.add_argument("--notarized", action="store_true")
    app.add_argument("--gatekeeper", action="store_true")
    app.add_argument("--smoke-launch", action="store_true")

    artifact = subparsers.add_parser(
        "verify", help="verify an archived app and checksum"
    )
    artifact.add_argument("--artifact", required=True, type=Path)
    artifact.add_argument("--checksum", required=True, type=Path)
    artifact.add_argument("--version", required=True)
    artifact.add_argument("--architectures", nargs="+", required=True)
    artifact.add_argument(
        "--signature", choices=("ad-hoc", "developer-id"), required=True
    )
    artifact.add_argument("--notarized", action="store_true")
    artifact.add_argument("--gatekeeper", action="store_true")
    artifact.add_argument("--smoke-launch", action="store_true")
    artifact.set_defaults(handler=verify_artifact)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "version":
            actual = project_version()
            if args.expect:
                validate_version(args.expect, actual)
            print(actual)
        elif args.command == "verify-app":
            verify_app(
                args.app,
                version=args.version,
                architectures=validate_architectures(args.architectures),
                signature=args.signature,
                notarized=args.notarized,
                gatekeeper=args.gatekeeper,
                smoke_launch=args.smoke_launch,
            )
            print(f"Verified {args.app}")
        else:
            args.handler(args)
    except (
        ReleaseError,
        OSError,
        plistlib.InvalidFileException,
        zipfile.BadZipFile,
    ) as error:
        print(f"release error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
