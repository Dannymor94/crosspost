"""Private, operator-invoked Crosspost recovery bundle with authenticated inventory.

No service is started and no network requests are made by this script. Non-SQLite
assets must be quiesced before a bundle is made; encrypted offsite custody and the
actual no-egress application drill are separate operator responsibilities.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

import sqlite_backup

VERSION = 1
MANIFEST = "recovery_manifest.json"
MAX_FILES = 200_000
MAX_BYTES = 1_000_000_000_000  # Fail closed if the operator selected an unexpected tree.
ALLOWED_SETTINGS = frozenset({"browser_headless"})


class BundleError(Exception):
    """Expected operator error that must not include secret contents."""


def _auth(meta: dict, key: bytes) -> str:
    signing_key = hmac.new(key, b"crosspost-recovery-bundle-v1", hashlib.sha256).digest()
    data = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(signing_key, data, hashlib.sha256).hexdigest()


def _ensure_real_dir(path: Path) -> Path:
    try:
        return sqlite_backup._private_dir(path)
    except sqlite_backup.BackupError as exc:
        raise BundleError(str(exc)) from exc


def _require_real_dir(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BundleError("required media/browser directory missing or symlinked")


def _require_valid_settings(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise BundleError("required runtime settings file missing or symlinked")
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError) as exc:
        raise BundleError("invalid runtime settings JSON") from exc
    if (
        not isinstance(settings, dict)
        or not set(settings).issubset(ALLOWED_SETTINGS)
        or ("browser_headless" in settings and not isinstance(settings["browser_headless"], bool))
    ):
        raise BundleError("runtime settings contain unexpected keys/types; review separately")


def _settings_present(path: Path) -> bool:
    """An absent settings.json is a supported fresh-deployment state."""
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise BundleError("runtime settings parent directory missing or symlinked")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise BundleError("runtime settings must be a regular file when present")
    _require_valid_settings(path)
    return True


def _publish_directory_exclusive(staging: Path, target: Path) -> None:
    """Publish a completed bundle atomically without replacing even an empty directory.

    Linux renameat2(RENAME_NOREPLACE) matches the project's Docker/VPS target.
    Fail closed elsewhere instead of silently falling back to os.rename(), which
    can overwrite an existing empty directory during a check/rename race.
    """
    if sys.platform != "linux":
        raise BundleError("atomic directory publication requires Linux renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BundleError("atomic renameat2 unavailable; refusing unsafe publication")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(staging), -100, os.fsencode(target), 1)
    if result == 0:
        return
    code = ctypes.get_errno()
    if code in (errno.EEXIST, errno.ENOTEMPTY):
        raise BundleError("recovery destination already exists; refusing overwrite")
    raise BundleError(f"atomic directory publication failed (errno={code})")


def _safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise BundleError("unsafe recovery manifest path")
    return Path(*pure.parts)


def _read_file(source: Path, destination: Path | None = None) -> tuple[str, int]:
    """Stream a regular file without following its leaf symlink; keep copies private."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise BundleError("recovery file missing or unreadable") from exc
    digest = hashlib.sha256()
    length = 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleError("recovery file is not a regular file")
        if destination is None:
            with os.fdopen(fd, "rb", closefd=False) as src:
                while chunk := src.read(1024 * 1024):
                    length += len(chunk)
                    if length > MAX_BYTES:
                        raise BundleError("recovery file exceeds safety limit")
                    digest.update(chunk)
        else:
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with os.fdopen(fd, "rb", closefd=False) as src, destination.open("xb") as dst:
                os.chmod(destination, 0o600)
                while chunk := src.read(1024 * 1024):
                    length += len(chunk)
                    if length > MAX_BYTES:
                        raise BundleError("recovery file exceeds safety limit")
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
    finally:
        os.close(fd)
    return digest.hexdigest(), length


def _walk_files(root: Path) -> list[Path]:
    _require_real_dir(root)
    paths: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            entry = current_path / name
            if entry.is_symlink() or not entry.is_dir():
                raise BundleError("recovery input contains symlink/non-directory")
        for name in names:
            entry = current_path / name
            if entry.is_symlink() or not entry.is_file():
                raise BundleError("recovery input contains symlink/non-file")
            paths.append(entry.relative_to(root))
            if len(paths) > MAX_FILES:
                raise BundleError("recovery inventory file count exceeds safety limit")
    return sorted(paths, key=lambda item: item.as_posix())


def _contained_or_equal(parent: Path, candidate: Path) -> bool:
    return candidate == parent or candidate.is_relative_to(parent)


def _copy_and_list(source: Path, target: Path, inventory: list[dict], total: list[int]) -> None:
    checksum, size = _read_file(source, target)
    total[0] += size
    if total[0] > MAX_BYTES:
        raise BundleError("recovery bundle exceeds safety limit")
    inventory.append({"path": target.as_posix(), "sha256": checksum, "size_bytes": size})


def backup_bundle(
    database: Path,
    media: Path,
    profiles: Path,
    settings: Path,
    output: Path,
    quiesced: bool,
) -> Path:
    if not quiesced:
        raise BundleError("asset backup requires explicit --quiesced acknowledgment")
    key = sqlite_backup._vault_key()
    sqlite_backup._integrity_and_schema(database)
    _require_real_dir(media)
    _require_real_dir(profiles)
    settings_present = _settings_present(settings)
    media_root = media.resolve()
    profile_root = profiles.resolve()
    if _contained_or_equal(media_root, profile_root) or _contained_or_equal(
        profile_root, media_root
    ):
        raise BundleError("media and browser profile source directories must be disjoint")
    for root in (media_root, profile_root):
        if _contained_or_equal(root, database.resolve()) or _contained_or_equal(
            root, settings.resolve()
        ):
            raise BundleError("media/browser input must not include database or runtime settings")
    output_resolved = output.resolve()
    for source in (media, profiles, settings.parent, database.parent):
        source_resolved = source.resolve()
        if _contained_or_equal(source_resolved, output_resolved):
            raise BundleError("recovery destination overlaps an input directory")
    _ensure_real_dir(output)
    staging = output / f".recovery-staging-{uuid4().hex}"
    staging.mkdir(mode=0o700)
    target = (
        output
        / f"crosspost-recovery-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:12]}"
    )
    inventory: list[dict] = []
    total = [0]
    try:
        db_dir = _ensure_real_dir(staging / "database")
        snapshot = sqlite_backup.backup(database, db_dir, keep=0)
        for item in (snapshot, snapshot.with_suffix(".json")):
            checksum, size = _read_file(item)
            total[0] += size
            inventory.append(
                {
                    "path": item.relative_to(staging).as_posix(),
                    "sha256": checksum,
                    "size_bytes": size,
                }
            )
        for group, source in (("media", media), ("profiles", profiles)):
            dest_root = staging / group
            dest_root.mkdir(mode=0o700)
            for relative in _walk_files(source):
                _copy_and_list(source / relative, dest_root / relative, inventory, total)
                inventory[-1]["path"] = (Path(group) / relative).as_posix()
        if settings_present:
            _copy_and_list(settings, staging / "settings.json", inventory, total)
            inventory[-1]["path"] = "settings.json"
        if len(inventory) > MAX_FILES or total[0] > MAX_BYTES:
            raise BundleError("recovery inventory exceeds safety limit")
        meta = {
            "version": VERSION,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "sqlite_snapshot": snapshot.relative_to(staging).as_posix(),
            "vault_key_fingerprint_sha256": hashlib.sha256(key).hexdigest(),
            "keys_embedded": False,
            "encrypted": False,
            "quiesced_by_operator": True,
            "settings_present": settings_present,
            "files": sorted(inventory, key=lambda item: item["path"]),
        }
        meta["hmac_sha256"] = _auth(meta, key)
        manifest = staging / MANIFEST
        with manifest.open("x", encoding="utf-8") as stream:
            os.chmod(manifest, 0o600)
            json.dump(meta, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists() or target.is_symlink():
            raise BundleError("recovery bundle destination already exists")
        _publish_directory_exclusive(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def verify_bundle(bundle: Path) -> dict:
    key = sqlite_backup._vault_key()
    _require_real_dir(bundle)
    manifest = bundle / MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise BundleError("recovery bundle manifest missing or symlinked")
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise BundleError("recovery bundle manifest is invalid") from exc
    if not isinstance(meta, dict):
        raise BundleError("recovery bundle manifest contract invalid")
    signature = meta.get("hmac_sha256")
    unsigned = {name: value for name, value in meta.items() if name != "hmac_sha256"}
    if (
        meta.get("version") != VERSION
        or meta.get("encrypted") is not False
        or meta.get("keys_embedded") is not False
        or meta.get("quiesced_by_operator") is not True
        or type(meta.get("settings_present")) is not bool
        or meta.get("vault_key_fingerprint_sha256") != hashlib.sha256(key).hexdigest()
        or not isinstance(signature, str)
        or not hmac.compare_digest(_auth(unsigned, key), signature)
    ):
        raise BundleError("recovery bundle authentication failed or key mismatch")
    files = meta.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise BundleError("recovery bundle file inventory is invalid")
    expected: set[str] = set()
    size_sum = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise BundleError("recovery file record is invalid")
        path, sha, size = item["path"], item["sha256"], item["size_bytes"]
        if not isinstance(path, str):
            raise BundleError("recovery file path invalid")
        relative = _safe_relative(path)
        if (
            path in expected
            or not isinstance(sha, str)
            or len(sha) != 64
            or any(ch not in "0123456789abcdef" for ch in sha)
            or type(size) is not int
            or size < 0
        ):
            raise BundleError("recovery file record duplicate/invalid")
        expected.add(path)
        size_sum += size
        if size_sum > MAX_BYTES:
            raise BundleError("recovery bundle exceeds safety limit")
        actual_sha, actual_size = _read_file(bundle / relative)
        if (actual_sha, actual_size) != (sha, size):
            raise BundleError("recovery file checksum/size mismatch")
    snapshot_name = meta.get("sqlite_snapshot")
    if (
        not isinstance(snapshot_name, str)
        or snapshot_name not in expected
        or not snapshot_name.startswith("database/")
        or not sqlite_backup.ARCHIVE_RE.fullmatch(Path(snapshot_name).name)
        or ("settings.json" in expected) != meta["settings_present"]
    ):
        raise BundleError("recovery bundle missing required artifacts")
    if not (any(path.startswith("database/") and path.endswith(".json") for path in expected)):
        raise BundleError("recovery bundle missing SQLite manifest")
    if not (bundle / "media").is_dir() or not (bundle / "profiles").is_dir():
        raise BundleError("recovery bundle missing asset groups")
    actual = {path.as_posix() for path in _walk_files(bundle) if path.as_posix() != MANIFEST}
    if expected != actual:
        raise BundleError("recovery bundle contains missing/extra files")
    sqlite_backup.verify(bundle / _safe_relative(snapshot_name))
    if meta["settings_present"]:
        _require_valid_settings(bundle / "settings.json")
    return meta


def restore_bundle(bundle: Path, destination: Path) -> Path:
    meta = verify_bundle(bundle)
    if destination.exists() or destination.is_symlink():
        raise BundleError("restore destination exists; use a NEW staging directory")
    if _contained_or_equal(bundle.resolve(), destination.resolve()):
        raise BundleError("restore destination overlaps recovery input")
    _ensure_real_dir(destination.parent)
    staging = destination.parent / f".recovery-restore-{uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        for group in ("database", "media", "profiles"):
            (staging / group).mkdir(mode=0o700)
        for item in meta["files"]:
            path = _safe_relative(item["path"])
            checksum, size = _read_file(bundle / path, staging / path)
            if (checksum, size) != (item["sha256"], item["size_bytes"]):
                raise BundleError("restored file did not match authenticated inventory")
        _read_file(bundle / MANIFEST, staging / MANIFEST)
        verify_bundle(staging)
        _publish_directory_exclusive(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Crosspost full-service recovery bundle")
    commands = parser.add_subparsers(dest="action", required=True)
    b = commands.add_parser("backup")
    b.add_argument("--db", type=Path, required=True)
    b.add_argument("--media-dir", type=Path, required=True)
    b.add_argument("--profiles-dir", type=Path, required=True)
    b.add_argument("--settings", type=Path, required=True)
    b.add_argument("--dest", type=Path, required=True)
    b.add_argument(
        "--quiesced", action="store_true", help="assert app/uploads/browser writes stopped"
    )
    v = commands.add_parser("verify")
    v.add_argument("--bundle", type=Path, required=True)
    r = commands.add_parser("restore")
    r.add_argument("--bundle", type=Path, required=True)
    r.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "backup":
            result = backup_bundle(
                args.db, args.media_dir, args.profiles_dir, args.settings, args.dest, args.quiesced
            )
            print(f"BUNDLE_OK {result}")
        elif args.action == "verify":
            verify_bundle(args.bundle)
            print("BUNDLE_VERIFY_OK")
        else:
            restore_bundle(args.bundle, args.target)
            print("STAGING_RESTORE_OK")
        return 0
    except (BundleError, sqlite_backup.BackupError) as exc:
        print(f"RECOVERY_ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        # Avoid leaking private source file names from low-level OS exceptions.
        print(f"RECOVERY_ERROR: operation failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
