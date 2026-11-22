"""Full-service recovery bundle: synthetic data only, never real customer assets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "recovery_bundle.py"


@pytest.fixture
def key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _run(tmp_path: Path, key: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["VAULT_KEY"] = key
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _sources(tmp_path: Path) -> tuple[Path, sqlite3.Connection, Path, Path, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = runtime / "live.sqlite3"
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    for name in ("profiles", "credentials", "publications", "alembic_version"):
        conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO profiles(name) VALUES ('profile-a')")
    conn.commit()
    media = runtime / "media"
    (media / "assets").mkdir(parents=True)
    (media / "assets" / "post.jpg").write_bytes(b"synthetic-image")
    profiles = runtime / "profiles"
    (profiles / "vk").mkdir(parents=True)
    (profiles / "vk" / "storage_state.json").write_text('{"cookies":[]}', encoding="utf-8")
    settings = runtime / "settings.json"
    settings.write_text('{"browser_headless":true}', encoding="utf-8")
    return database, conn, media, profiles, settings


def _bundle(
    tmp_path: Path,
    key: str,
    database: Path,
    media: Path,
    profiles: Path,
    settings: Path,
    *extra: str,
) -> Path | subprocess.CompletedProcess[str]:
    result = _run(
        tmp_path,
        key,
        "backup",
        "--db",
        str(database),
        "--media-dir",
        str(media),
        "--profiles-dir",
        str(profiles),
        "--settings",
        str(settings),
        "--dest",
        str(tmp_path / "backups"),
        *extra,
    )
    if "--quiesced" not in extra:
        return result
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip().removeprefix("BUNDLE_OK "))


def test_bundle_preserves_wal_commits_media_browser_state_and_settings(tmp_path, key):
    database, conn, media, profiles, settings = _sources(tmp_path)
    conn.execute("INSERT INTO profiles(name) VALUES ('committed-in-wal')")
    conn.commit()
    assert database.with_suffix(".sqlite3-wal").exists()
    bundle = _bundle(tmp_path, key, database, media, profiles, settings, "--quiesced")
    assert isinstance(bundle, Path)
    manifest_text = (bundle / "recovery_manifest.json").read_text(encoding="utf-8")
    meta = json.loads(manifest_text)
    assert "VAULT_KEY" not in manifest_text
    assert key not in manifest_text
    assert meta["encrypted"] is False  # Requires external encrypted offsite custody.
    assert meta["keys_embedded"] is False
    assert any(f["path"] == "media/assets/post.jpg" for f in meta["files"])
    assert any(f["path"] == "profiles/vk/storage_state.json" for f in meta["files"])
    assert _run(tmp_path, key, "verify", "--bundle", str(bundle)).returncode == 0

    target = tmp_path / "restored" / "new-runtime"
    restored = _run(tmp_path, key, "restore", "--bundle", str(bundle), "--target", str(target))
    assert restored.returncode == 0, restored.stderr
    db_name = meta["sqlite_snapshot"]
    with sqlite3.connect(target / db_name) as con:
        assert con.execute("SELECT name FROM profiles ORDER BY id").fetchall() == [
            ("profile-a",),
            ("committed-in-wal",),
        ]
    assert (target / "media/assets/post.jpg").read_bytes() == b"synthetic-image"
    assert (target / "profiles/vk/storage_state.json").read_text() == '{"cookies":[]}'
    assert json.loads((target / "settings.json").read_text()) == {"browser_headless": True}
    assert _run(tmp_path, key, "verify", "--bundle", str(target)).returncode == 0
    if os.name == "posix":
        assert (target.stat().st_mode & 0o077) == 0
        for path in (target / "settings.json", target / "media/assets/post.jpg"):
            assert (path.stat().st_mode & 0o077) == 0
    conn.close()


def test_bundle_requires_quiesced_assertion(tmp_path, key):
    database, conn, media, profiles, settings = _sources(tmp_path)
    result = _bundle(tmp_path, key, database, media, profiles, settings)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 2
    assert "quiesced" in result.stderr
    assert not (tmp_path / "backups").exists()
    conn.close()


@pytest.mark.parametrize("corruption", ["tamper", "remove", "extra", "missing_db_manifest"])
def test_bundle_corrupt_or_incomplete_refuses_restore(tmp_path, key, corruption):
    database, conn, media, profiles, settings = _sources(tmp_path)
    bundle = _bundle(tmp_path, key, database, media, profiles, settings, "--quiesced")
    assert isinstance(bundle, Path)
    if corruption == "tamper":
        (bundle / "media/assets/post.jpg").write_bytes(b"tampered")
    elif corruption == "remove":
        (bundle / "profiles/vk/storage_state.json").unlink()
    elif corruption == "extra":
        (bundle / "media/assets/untracked.txt").write_text("extra", encoding="utf-8")
    else:
        for file in (bundle / "database").glob("*.json"):
            file.unlink()
    target = tmp_path / "restored" / "new-runtime"
    result = _run(tmp_path, key, "restore", "--bundle", str(bundle), "--target", str(target))
    assert result.returncode == 2
    assert not target.exists()
    conn.close()


def test_bundle_wrong_key_cannot_restore_and_existing_target_never_clobbered(tmp_path, key):
    database, conn, media, profiles, settings = _sources(tmp_path)
    bundle = _bundle(tmp_path, key, database, media, profiles, settings, "--quiesced")
    assert isinstance(bundle, Path)
    target = tmp_path / "restored" / "new-runtime"
    other_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    assert (
        _run(
            tmp_path, other_key, "restore", "--bundle", str(bundle), "--target", str(target)
        ).returncode
        == 2
    )
    assert not target.exists()
    target.mkdir(parents=True)
    sentinel = target / "never-clobber.txt"
    sentinel.write_text("keep", encoding="utf-8")
    assert (
        _run(tmp_path, key, "restore", "--bundle", str(bundle), "--target", str(target)).returncode
        == 2
    )
    assert sentinel.read_text() == "keep"
    conn.close()


def test_bundle_rejects_source_symlink_and_unknown_settings(tmp_path, key):
    database, conn, media, profiles, settings = _sources(tmp_path)
    link = media / "assets" / "indirect.jpg"
    link.symlink_to(media / "assets" / "post.jpg")
    result = _run(
        tmp_path,
        key,
        "backup",
        "--db",
        str(database),
        "--media-dir",
        str(media),
        "--profiles-dir",
        str(profiles),
        "--settings",
        str(settings),
        "--dest",
        str(tmp_path / "backups"),
        "--quiesced",
    )
    assert result.returncode == 2
    assert "symlink" in result.stderr
    assert not list((tmp_path / "backups").glob("crosspost-recovery-*"))
    link.unlink()
    settings.write_text('{"browser_headless":true,"VAULT_KEY":"do-not-copy"}', encoding="utf-8")
    bad = _run(
        tmp_path,
        key,
        "backup",
        "--db",
        str(database),
        "--media-dir",
        str(media),
        "--profiles-dir",
        str(profiles),
        "--settings",
        str(settings),
        "--dest",
        str(tmp_path / "backups"),
        "--quiesced",
    )
    assert bad.returncode == 2
    assert "unexpected keys/types" in bad.stderr
    assert "do-not-copy" not in bad.stderr
    conn.close()


def test_bundle_rejects_output_inside_runtime_sources(tmp_path, key):
    database, conn, media, profiles, settings = _sources(tmp_path)
    result = _run(
        tmp_path,
        key,
        "backup",
        "--db",
        str(database),
        "--media-dir",
        str(media),
        "--profiles-dir",
        str(profiles),
        "--settings",
        str(settings),
        "--dest",
        str(media / "backups"),
        "--quiesced",
    )
    assert result.returncode == 2
    assert "overlaps" in result.stderr
    assert not (media / "backups").exists()
    conn.close()


def test_bundle_rejects_media_root_that_accidentally_includes_runtime_secrets(tmp_path, key):
    database, conn, _media, profiles, settings = _sources(tmp_path)
    runtime = database.parent
    (runtime / ".env").write_text("VAULT_KEY=never-copy-this", encoding="utf-8")
    bad = _run(
        tmp_path,
        key,
        "backup",
        "--db",
        str(database),
        "--media-dir",
        str(runtime),
        "--profiles-dir",
        str(profiles),
        "--settings",
        str(settings),
        "--dest",
        str(tmp_path / "backups"),
        "--quiesced",
    )
    assert bad.returncode == 2
    assert "disjoint" in bad.stderr or "must not include" in bad.stderr
    assert "never-copy-this" not in bad.stderr
    assert not (tmp_path / "backups").exists()
    conn.close()


def test_restore_rejects_authenticated_manifest_path_traversal(tmp_path, key):
    database, conn, media, profiles, settings = _sources(tmp_path)
    bundle = _bundle(tmp_path, key, database, media, profiles, settings, "--quiesced")
    assert isinstance(bundle, Path)
    manifest_path = bundle / "recovery_manifest.json"
    meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    meta["files"][0]["path"] = "../escape.txt"
    decoded_key = base64.urlsafe_b64decode(key)
    signing_key = hmac.new(decoded_key, b"crosspost-recovery-bundle-v1", hashlib.sha256).digest()
    unsigned = {k: v for k, v in meta.items() if k != "hmac_sha256"}
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    meta["hmac_sha256"] = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
    manifest_path.write_text(json.dumps(meta), encoding="utf-8")
    target = tmp_path / "staging" / "recovered"
    bad = _run(tmp_path, key, "restore", "--bundle", str(bundle), "--target", str(target))
    assert bad.returncode == 2
    assert "unsafe recovery manifest path" in bad.stderr
    assert not target.exists()
    assert not (tmp_path / "escape.txt").exists()
    conn.close()


def test_absent_settings_json_is_authenticated_and_stays_absent_after_restore(tmp_path, key):
    database, conn, media, profiles, settings = _sources(tmp_path)
    settings.unlink()  # RuntimeSettings._load() legitimately defaults to {}.
    bundle = _bundle(tmp_path, key, database, media, profiles, settings, "--quiesced")
    assert isinstance(bundle, Path)
    manifest = json.loads((bundle / "recovery_manifest.json").read_text())
    assert manifest["settings_present"] is False
    assert "settings.json" not in {entry["path"] for entry in manifest["files"]}
    assert _run(tmp_path, key, "verify", "--bundle", str(bundle)).returncode == 0
    target = tmp_path / "staging" / "recovered"
    restored = _run(tmp_path, key, "restore", "--bundle", str(bundle), "--target", str(target))
    assert restored.returncode == 0, restored.stderr
    assert not (target / "settings.json").exists()
    assert _run(tmp_path, key, "verify", "--bundle", str(target)).returncode == 0
    conn.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux renameat2 RENAME_NOREPLACE contract")
def test_rename_race_cannot_clobber_newly_created_empty_directory(tmp_path, key, monkeypatch):
    database, conn, media, profiles, settings = _sources(tmp_path)
    bundle = _bundle(tmp_path, key, database, media, profiles, settings, "--quiesced")
    assert isinstance(bundle, Path)
    monkeypatch.setenv("VAULT_KEY", key)
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    module = importlib.import_module("recovery_bundle")
    real_publish = module._publish_directory_exclusive
    destination = tmp_path / "staging" / "recovered"

    def destination_appears_just_before_publish(source: Path, target: Path) -> None:
        assert target == destination
        target.mkdir(mode=0o700)  # Existing empty dirs are replaceable by os.rename().
        real_publish(source, target)

    monkeypatch.setattr(
        module, "_publish_directory_exclusive", destination_appears_just_before_publish
    )
    with pytest.raises(module.BundleError, match="already exists"):
        module.restore_bundle(bundle, destination)
    assert destination.is_dir()
    assert not list(destination.iterdir())
    assert not list(destination.parent.glob(".recovery-restore-*"))
    conn.close()
