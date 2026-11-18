"""Operator-only, offline-artifact backup / verification / restore for Crosspost SQLite.

Uses SQLite's online backup API: copying db.sqlite3 directly is unsafe in WAL mode.
The database contains encrypted credentials and client content. Backup files must
be private and the separately escrowed VAULT_KEY is required for recovery.

No dependency on application imports. This script is intentionally outside src/:
production code must not import sqlite3 directly (project contract).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

ARCHIVE_RE = re.compile(r"^crosspost-\d{8}T\d{6}Z-[0-9a-f]{12}\.sqlite3$")
REQUIRED_TABLES = frozenset({"profiles", "credentials", "publications", "alembic_version"})
MANIFEST_VERSION = 1


class BackupError(Exception):
    """A safe-to-report operator error."""


def _vault_key() -> bytes:
    """Decode a supplied VAULT_KEY without ever storing the key in an archive."""
    key = os.environ.get("VAULT_KEY", "")
    if not key:
        raise BackupError("VAULT_KEY environment variable is required; escrow key separately")
    try:
        decoded = base64.b64decode(key, altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise BackupError("VAULT_KEY is not a valid Fernet key") from exc
    if len(decoded) != 32:
        raise BackupError("VAULT_KEY is not a valid Fernet key")
    return decoded


def _sign_manifest(meta: dict, key: bytes) -> str:
    # Separate the authentication usage from Fernet by deriving a dedicated HMAC key.
    auth_key = hmac.new(key, b"crosspost-backup-manifest-v1", hashlib.sha256).digest()
    payload = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(auth_key, payload, hashlib.sha256).hexdigest()


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise BackupError("SQLite source/snapshot must be an existing regular file (not symlink)")
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _integrity_and_schema(path: Path) -> None:
    with closing(_read_only(path)) as conn:
        status = conn.execute("PRAGMA integrity_check").fetchone()
        if status != ("ok",):
            raise BackupError("SQLite integrity_check failed")
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not REQUIRED_TABLES.issubset(names):
            raise BackupError("not a recognized migrated Crosspost database")


def _sha256_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise BackupError("backup/restore directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise BackupError("backup/restore destination must be a real directory")
    if os.name == "posix" and (path.stat().st_mode & 0o077):
        raise BackupError("backup/restore directory permissions must be 0700")
    return path


def _temp_path(directory: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".crosspost-staging-", suffix=".tmp", dir=directory)
    os.chmod(name, 0o600)
    os.close(fd)
    return Path(name)


def _fsync(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _publish_exclusive(staged: Path, target: Path) -> None:
    """Never overwrite an existing backup/restore target, even under races."""
    try:
        os.link(staged, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise BackupError("destination already exists; refusing to overwrite") from exc


def _manifest_path(snapshot: Path) -> Path:
    return snapshot.with_suffix(".json")


def backup(source: Path, directory: Path, keep: int) -> Path:
    vault_key = _vault_key()
    fingerprint = hashlib.sha256(vault_key).hexdigest()
    if keep < 0:
        raise BackupError("--keep must be >= 0 (0 disables rotation)")
    # Validate source first, without touching destination.
    _integrity_and_schema(source)
    directory = _private_dir(directory)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"crosspost-{stamp}-{uuid4().hex[:12]}.sqlite3"
    staged = _temp_path(directory)
    created_snapshot = False
    created_manifest = False
    try:
        with closing(_read_only(source)) as live, closing(sqlite3.connect(staged)) as destination:
            live.backup(destination, pages=512, sleep=0.05)
            # Produce a single self-contained DB file, not a WAL + sidecars.
            mode = destination.execute("PRAGMA journal_mode=DELETE").fetchone()
            if mode is None or str(mode[0]).lower() != "delete":
                raise BackupError("could not switch snapshot to self-contained journal mode")
        _integrity_and_schema(staged)
        _fsync(staged)
        checksum, size = _sha256_size(staged)
        manifest = {
            "version": MANIFEST_VERSION,
            "file": target.name,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "sha256": checksum,
            "size_bytes": size,
            "vault_key_fingerprint_sha256": fingerprint,
            "key_embedded": False,
        }
        manifest["hmac_sha256"] = _sign_manifest(manifest, vault_key)
        _publish_exclusive(staged, target)
        created_snapshot = True
        manifest_staged = _temp_path(directory)
        try:
            manifest_staged.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            _fsync(manifest_staged)
            _publish_exclusive(manifest_staged, _manifest_path(target))
            created_manifest = True
        finally:
            manifest_staged.unlink(missing_ok=True)
    except Exception:
        if created_snapshot and not created_manifest:
            target.unlink(missing_ok=True)
        raise
    finally:
        staged.unlink(missing_ok=True)
    if keep:
        _rotate(directory, keep=keep, fingerprint=fingerprint)
    return target


def _load_manifest(snapshot: Path) -> dict:
    manifest_path = _manifest_path(snapshot)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError("snapshot manifest is missing or is a symlink")
    try:
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BackupError("snapshot manifest is corrupt") from exc
    if (
        not isinstance(meta, dict)
        or meta.get("version") != MANIFEST_VERSION
        or meta.get("file") != snapshot.name
        or not isinstance(meta.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
        or not isinstance(meta.get("size_bytes"), int)
        or not isinstance(meta.get("vault_key_fingerprint_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", meta["vault_key_fingerprint_sha256"])
        or meta.get("key_embedded") is not False
        or not isinstance(meta.get("hmac_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", meta["hmac_sha256"])
        or not isinstance(meta.get("created_at_utc"), str)
    ):
        raise BackupError("snapshot manifest contract invalid")
    return meta


def verify(snapshot: Path) -> dict:
    if not ARCHIVE_RE.fullmatch(snapshot.name):
        raise BackupError("snapshot filename does not match crosspost archive format")
    vault_key = _vault_key()
    fingerprint = hashlib.sha256(vault_key).hexdigest()
    meta = _load_manifest(snapshot)
    if fingerprint != meta["vault_key_fingerprint_sha256"]:
        raise BackupError("VAULT_KEY fingerprint mismatch; refusing to restore")
    signed = {k: v for k, v in meta.items() if k != "hmac_sha256"}
    if not hmac.compare_digest(_sign_manifest(signed, vault_key), meta["hmac_sha256"]):
        raise BackupError("snapshot manifest authentication failed")
    if snapshot.is_symlink() or not snapshot.is_file():
        raise BackupError("snapshot is missing or is a symlink")
    checksum, size = _sha256_size(snapshot)
    if checksum != meta["sha256"] or size != meta["size_bytes"]:
        raise BackupError("snapshot checksum/size mismatch")
    _integrity_and_schema(snapshot)
    return meta


def restore(snapshot: Path, destination: Path) -> Path:
    verify(snapshot)  # No writes before key, checksum, schema, and integrity checks.
    if destination.exists() or destination.is_symlink():
        raise BackupError("restore target already exists; restore to a new path with service stopped")
    directory = _private_dir(destination.parent)
    if not directory.samefile(destination.parent):
        raise BackupError("unexpected restore directory")
    staged = _temp_path(directory)
    try:
        with snapshot.open("rb") as src, staged.open("wb") as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        _integrity_and_schema(staged)
        _publish_exclusive(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    return destination


def _rotate(directory: Path, keep: int, fingerprint: str) -> None:
    """Only prune complete archives for the same key; retain archives from older keys."""
    eligible: list[tuple[datetime, Path]] = []
    for item in directory.glob("crosspost-*.sqlite3"):
        if item.is_symlink() or not ARCHIVE_RE.fullmatch(item.name):
            continue
        try:
            meta = _load_manifest(item)
        except BackupError:
            continue
        if meta["file"] == item.name and meta["vault_key_fingerprint_sha256"] == fingerprint:
            try:
                created_at = datetime.fromisoformat(meta["created_at_utc"])
                if created_at.tzinfo is None:
                    continue
            except ValueError:
                continue
            eligible.append((created_at, item))
    eligible.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
    for _date, old in eligible[keep:]:
        _manifest_path(old).unlink()
        old.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crosspost SQLite online backup and safe restore")
    cmd = parser.add_subparsers(dest="action", required=True)
    b = cmd.add_parser("backup", help="consistent online snapshot into a private directory")
    b.add_argument("--db", type=Path, required=True, help="SQLite database file")
    b.add_argument("--dest", type=Path, required=True, help="0700 backup directory")
    b.add_argument("--keep", type=int, default=7, help="number of complete snapshots to keep")
    v = cmd.add_parser("verify", help="verify checksum, integrity, schema and VAULT_KEY")
    v.add_argument("--snapshot", type=Path, required=True)
    r = cmd.add_parser("restore", help="restore verified snapshot to a NEW database file")
    r.add_argument("--snapshot", type=Path, required=True)
    r.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "backup":
            out = backup(args.db, args.dest, args.keep)
            print(f"BACKUP_OK {out}")
        elif args.action == "verify":
            meta = verify(args.snapshot)
            print(f"VERIFY_OK {args.snapshot} sha256={meta['sha256']}")
        else:
            out = restore(args.snapshot, args.target)
            print(f"RESTORE_OK {out}")
        return 0
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        # Report a controlled reason; never include credential contents.
        print(f"BACKUP_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
