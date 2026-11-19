"""Contract tests for the standard-library operator backup CLI (T-207)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sqlite_backup.py"


@pytest.fixture
def key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _run(tmp_path: Path, key: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["VAULT_KEY"] = key
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _database(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "live.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    for name in ("profiles", "credentials", "publications", "alembic_version"):
        conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO profiles(name) VALUES ('existing-client')")
    conn.commit()
    return path, conn


def _backup(tmp_path: Path, key: str, db: Path, keep: int = 7) -> Path:
    result = _run(
        tmp_path,
        key,
        "backup",
        "--db",
        str(db),
        "--dest",
        str(tmp_path / "backups"),
        "--keep",
        str(keep),
    )
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip().removeprefix("BACKUP_OK "))


def test_online_wal_snapshot_preserves_committed_rows_and_manifest(tmp_path, key):
    db, live = _database(tmp_path)
    live.execute("INSERT INTO profiles(name) VALUES ('in-wal')")
    live.commit()  # Keep writer open and uncheckpointed WAL present during backup.
    assert db.with_suffix(".sqlite3-wal").exists()
    snapshot = _backup(tmp_path, key, db)
    with sqlite3.connect(snapshot) as saved:
        assert saved.execute("SELECT name FROM profiles ORDER BY id").fetchall() == [
            ("existing-client",),
            ("in-wal",),
        ]
    manifest = json.loads(snapshot.with_suffix(".json").read_text())
    assert manifest["sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert manifest["key_embedded"] is False
    assert len(manifest["hmac_sha256"]) == 64
    assert "VAULT_KEY" not in manifest
    assert key not in snapshot.with_suffix(".json").read_text()
    if os.name == "posix":
        assert (snapshot.stat().st_mode & 0o077) == 0
    assert _run(tmp_path, key, "verify", "--snapshot", str(snapshot)).returncode == 0
    live.close()


def test_restore_requires_matching_key_and_never_overwrites(tmp_path, key):
    db, live = _database(tmp_path)
    snapshot = _backup(tmp_path, key, db)
    target = tmp_path / "recovered" / "db.sqlite3"
    wrong_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    bad = _run(tmp_path, wrong_key, "restore", "--snapshot", str(snapshot), "--target", str(target))
    assert bad.returncode == 2
    assert not target.exists()
    ok = _run(tmp_path, key, "restore", "--snapshot", str(snapshot), "--target", str(target))
    assert ok.returncode == 0, ok.stderr
    with sqlite3.connect(target) as restored:
        assert restored.execute("SELECT name FROM profiles").fetchone() == ("existing-client",)
    again = _run(tmp_path, key, "restore", "--snapshot", str(snapshot), "--target", str(target))
    assert again.returncode == 2
    with sqlite3.connect(target) as restored:
        assert restored.execute("SELECT count(*) FROM profiles").fetchone() == (1,)
    live.close()


def test_corrupted_archive_is_rejected_before_target_creation(tmp_path, key):
    db, live = _database(tmp_path)
    snapshot = _backup(tmp_path, key, db)
    with snapshot.open("ab") as out:
        out.write(b"tampered")
    target = tmp_path / "recovered" / "db.sqlite3"
    result = _run(tmp_path, key, "restore", "--snapshot", str(snapshot), "--target", str(target))
    assert result.returncode == 2
    assert not target.exists()
    live.close()


def test_rotation_keeps_only_tool_owned_pairs_and_ignores_strays(tmp_path, key):
    db, live = _database(tmp_path)
    unrelated = tmp_path / "backups" / "other.sqlite3"
    first = _backup(tmp_path, key, db, keep=2)
    unrelated.write_text("must stay")
    _backup(tmp_path, key, db, keep=2)
    _backup(tmp_path, key, db, keep=2)
    assert not first.exists()
    assert not first.with_suffix(".json").exists()
    assert len(list((tmp_path / "backups").glob("crosspost-*.sqlite3"))) == 2
    assert unrelated.read_text() == "must stay"
    live.close()


def test_missing_or_unrecognized_db_never_creates_backup(tmp_path, key):
    backup_dir = tmp_path / "backups"
    missing = _run(
        tmp_path,
        key,
        "backup",
        "--db",
        str(tmp_path / "missing.sqlite3"),
        "--dest",
        str(backup_dir),
    )
    assert missing.returncode == 2
    assert not backup_dir.exists()
    bad_db = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(bad_db) as conn:
        conn.execute("CREATE TABLE unknown (id INT)")
    wrong = _run(tmp_path, key, "backup", "--db", str(bad_db), "--dest", str(backup_dir))
    assert wrong.returncode == 2
    assert not backup_dir.exists()


def test_backup_rejects_public_directory(tmp_path, key):
    db, live = _database(tmp_path)
    pub = tmp_path / "public"
    pub.mkdir(mode=0o755)
    pub.chmod(0o755)
    result = _run(tmp_path, key, "backup", "--db", str(db), "--dest", str(pub))
    if os.name == "posix":
        assert result.returncode == 2
        assert not list(pub.glob("crosspost-*"))
    live.close()


def test_manifest_tamper_cannot_be_re_signed_without_vault_key(tmp_path, key):
    db, live = _database(tmp_path)
    snapshot = _backup(tmp_path, key, db)
    manifest_path = snapshot.with_suffix(".json")
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at_utc"] = "2099-01-01T00:00:00+00:00"
    manifest_path.write_text(json.dumps(manifest))
    target = tmp_path / "recovered" / "db.sqlite3"
    result = _run(tmp_path, key, "restore", "--snapshot", str(snapshot), "--target", str(target))
    assert result.returncode == 2
    assert "authentication failed" in result.stderr
    assert not target.exists()
    live.close()


def test_uncommitted_wal_transaction_not_in_snapshot(tmp_path, key):
    db, live = _database(tmp_path)
    live.execute("INSERT INTO profiles(name) VALUES ('UNCOMMITTED')")
    # Open WAL writer remains uncommitted in this process while the separate
    # backup subprocess takes the snapshot (SQLite readers must see only commits).
    snapshot = _backup(tmp_path, key, db)
    live.rollback()
    with sqlite3.connect(snapshot) as saved:
        assert saved.execute("SELECT name FROM profiles").fetchall() == [("existing-client",)]
    live.close()


def test_rotation_does_not_count_corrupt_newer_archive(tmp_path, key):
    db, live = _database(tmp_path)
    oldest = _backup(tmp_path, key, db, keep=2)
    bad_newer = _backup(tmp_path, key, db, keep=2)
    with bad_newer.open("ab") as out:
        out.write(b"tampered")
    newest = _backup(tmp_path, key, db, keep=2)
    assert oldest.exists()  # only newest + oldest are valid and count toward retention
    assert newest.exists()
    assert bad_newer.exists()  # bad archive is not silently deleted; operator investigates
    live.close()


def test_restore_rejects_mutation_between_verify_and_copy(tmp_path, key, monkeypatch):
    """Only bytes authenticated by the manifest may become a restored database."""
    import importlib.util

    db, live = _database(tmp_path)
    snapshot = _backup(tmp_path, key, db)
    monkeypatch.setenv("VAULT_KEY", key)
    spec = importlib.util.spec_from_file_location("crosspost_sqlite_backup_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    original_verify = module.verify

    def mutate_after_verified(path):
        meta = original_verify(path)
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO profiles(name) VALUES ('UNSIGNED_AFTER_VERIFY')")
        return meta

    monkeypatch.setattr(module, "verify", mutate_after_verified)
    destination = tmp_path / "recovered" / "db.sqlite3"
    with pytest.raises(module.BackupError, match="authenticated manifest"):
        module.restore(snapshot, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(".crosspost-staging-*"))
    live.close()
