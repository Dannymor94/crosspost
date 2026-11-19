"""INT-01: integrated migrated DB, retained media, read-only audit and WAL recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from crosspost.adapters.base import FailureKind
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState, CredentialKind, PublicationStatus
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.vault import get_vault
from crosspost.orchestrator.breaker import BreakerPolicy, CircuitBreakerRepository

ROOT = Path(__file__).resolve().parents[2]


async def test_int01_migrated_wal_backup_restore_and_read_only_media_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    db = tmp_path / "live.sqlite3"
    media = tmp_path / "media"
    photo = media / "publication" / "photo.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"synthetic retained media")
    orphan = media / "orphan" / "keep.jpg"
    orphan.parent.mkdir()
    orphan.write_bytes(b"candidate is never permission to delete")
    url = f"sqlite+aiosqlite:///{db}"
    engine = await create_engine_and_tables(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        profiles = ProfileRepository(session, vault=get_vault())
        profile = await profiles.create_profile("synthetic")
        pid = profile.id
        await profiles.set_credential(pid, "telegram", CredentialKind.API_TOKEN, "synthetic-token")
        await profiles.upsert_connection(pid, "telegram", ConnectionState.BANNED)
        publications = PublicationRepository(session, profile_id=pid)
        await publications.set_status("publication", "telegram", PublicationStatus.SUBMITTED)
        await publications.create_scheduled(
            content_type="post",
            text="synthetic",
            title=None,
            media_paths=[str(photo)],
            channels=["telegram"],
            scheduled_at=datetime(2026, 1, 1),
        )
        breaker = CircuitBreakerRepository(session)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        permit = await breaker.acquire(pid, "telegram", now=now, policy=BreakerPolicy())
        assert permit is not None
        await breaker.settle(
            pid, "telegram", permit, now=now, policy=BreakerPolicy(), failure=FailureKind.RATE_LIMIT
        )
    await engine.dispose()

    def run(*args):
        result = subprocess.run(
            [sys.executable, *map(str, args)],
            cwd=tmp_path,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    # Keep a real writer open: the committed row must live in the WAL at backup time.
    with sqlite3.connect(db) as live:
        assert live.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO profiles(name) VALUES ('committed-in-wal')")
        live.commit()
        files = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), photo, orphan]
        assert all(path.exists() for path in files)

        def footprint():
            return {
                str(path): (
                    path.stat().st_size,
                    # SQLite readers update SHM read marks (walformat.html §2.3.1).
                    # Linux may reflect that in mtime. SHM is coordination state,
                    # not database content; DB/WAL/media must remain byte-identical.
                    None if path.name.endswith("-shm") else path.stat().st_mtime_ns,
                    None
                    if path.name.endswith("-shm")
                    else hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in files
            }

        before = footprint()
        audit = json.loads(
            run(
                "-m",
                "crosspost.media.diagnostics",
                "--media-dir",
                media,
                "--db-url",
                url,
                "--json",
            )
        )
        assert audit["total_files"] == 2
        assert audit["orphan_candidates"] == ["orphan"]
        assert footprint() == before
        backup = ROOT / "scripts" / "sqlite_backup.py"
        snapshot = Path(
            run(backup, "backup", "--db", db, "--dest", tmp_path / "backups")
            .strip()
            .removeprefix("BACKUP_OK ")
        )
        run(backup, "verify", "--snapshot", snapshot)
        target = tmp_path / "restored" / "db.sqlite3"
        run(backup, "restore", "--snapshot", snapshot, "--target", target)
        assert photo.read_bytes() == b"synthetic retained media" and orphan.exists()
        with sqlite3.connect(target) as restored:
            assert restored.execute("SELECT name FROM profiles ORDER BY id").fetchall() == [
                ("synthetic",),
                ("committed-in-wal",),
            ]
            assert restored.execute("SELECT state FROM connections").fetchone() == ("banned",)
            assert restored.execute("SELECT status FROM publications").fetchone() == ("submitted",)
            assert restored.execute("SELECT state FROM circuit_breakers").fetchone() == ("open",)
            blob = restored.execute("SELECT blob FROM credentials").fetchone()[0]
            assert get_vault().decrypt(blob.encode()).decode() == "synthetic-token"
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restored.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "t203_fencing",
            )
