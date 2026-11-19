"""Mock-free synthetic filesystem/SQLite checks for the read-only T-213 audit."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from crosspost.media.diagnostics import _sqlite_path, audit_media, main


class TestMediaAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "media"
        self.root.mkdir()
        self.db_file = Path(self.tmp.name) / "store.sqlite3"
        self.db_url = f"sqlite+aiosqlite:///{self.db_file}"
        with sqlite3.connect(self.db_file) as conn:
            conn.execute(
                "CREATE TABLE scheduled_posts (id INTEGER PRIMARY KEY, "
                "profile_id INTEGER NOT NULL, media_paths TEXT)"
            )
            conn.execute("CREATE TABLE publications (profile_id INTEGER, publication_id TEXT)")

    def _add_scheduled(self, post_id: int, profile_id: int, paths: list[str]) -> None:
        with sqlite3.connect(self.db_file) as conn:
            conn.execute(
                "INSERT INTO scheduled_posts (id, profile_id, media_paths) VALUES (?, ?, ?)",
                (post_id, profile_id, json.dumps(paths)),
            )

    def _add_publication(self, profile_id: int, publication_id: str) -> None:
        with sqlite3.connect(self.db_file) as conn:
            conn.execute(
                "INSERT INTO publications (profile_id, publication_id) VALUES (?, ?)",
                (profile_id, publication_id),
            )

    def _file(self, key: str, name: str, data: bytes) -> Path:
        path = self.root / key / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_scheduled_shared_catalogues_are_not_orphans_across_profiles(self) -> None:
        shared = self._file("sched-abc", "pic.jpg", b"12345")
        self._add_scheduled(11, 1, [str(shared)])
        self._add_scheduled(12, 2, [str(shared)])
        self._file("orphan", "left.jpg", b"xx")
        report = audit_media(self.root, self.db_url)

        self.assertEqual(report.total_bytes, 7)
        self.assertEqual(report.total_files, 2)
        self.assertEqual(report.orphan_candidates, ("orphan",))
        self.assertEqual(report.directories[0].key, "sched-abc")
        self.assertEqual(report.directories[0].referenced_by, ("scheduled:1/11", "scheduled:2/12"))

    def test_immediate_publication_id_marks_directory_as_referenced(self) -> None:
        self._file("pub-abc", "photo.png", b"a")
        self._add_publication(7, "pub-abc")
        report = audit_media(self.root, self.db_url)
        self.assertEqual(report.orphan_candidates, ())
        self.assertEqual(report.directories[0].referenced_by, ("publication:7/pub-abc",))

    def test_scheduled_id_is_not_a_media_key_after_hard_delete(self) -> None:
        self._file("sched-old", "pic.jpg", b"data")
        self._add_publication(2, "schedpost-42")
        report = audit_media(self.root, self.db_url)
        # Lost scheduled_posts row makes this only a candidate, never safe to delete.
        self.assertEqual(report.orphan_candidates, ("sched-old",))

    def test_missing_scheduled_directory_and_outside_paths_are_reported(self) -> None:
        missing = self.root / "missing" / "image.jpg"
        self._add_scheduled(1, 5, [str(missing), "/not-the-media-root/file.jpg"])
        report = audit_media(self.root, self.db_url)
        self.assertEqual(report.missing_scheduled_directories, ("missing",))
        self.assertEqual(len(report.unmatched_database_paths), 1)
        self.assertIn("scheduled:5/1", report.unmatched_database_paths[0])

    def test_dotdot_escape_is_unmatched_not_missing_directory(self) -> None:
        outside = self.root / ".." / "outside" / "photo.jpg"
        self._add_scheduled(2, 5, [str(outside)])
        report = audit_media(self.root, self.db_url)
        self.assertEqual(report.missing_scheduled_directories, ())
        self.assertEqual(len(report.unmatched_database_paths), 1)
        self.assertIn("scheduled:5/2", report.unmatched_database_paths[0])

    def test_symlinks_are_never_followed_and_root_files_counted(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "huge").write_bytes(b"z" * 1000)
        folder = self.root / "dir"
        folder.mkdir()
        (folder / "safe").write_bytes(b"123")
        (folder / "escape").symlink_to(outside, target_is_directory=True)
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        (self.root / "loose.txt").write_bytes(b"xx")
        report = audit_media(self.root, self.db_url)
        self.assertEqual(report.total_bytes, 5)
        self.assertEqual(report.total_files, 2)
        self.assertEqual(report.root_file_bytes, 2)
        self.assertEqual(report.ignored_symlinks, 2)
        self.assertEqual(report.orphan_candidates, ("dir",))

    def test_invalid_media_paths_fails_closed_instead_of_false_orphans(self) -> None:
        with sqlite3.connect(self.db_file) as conn:
            conn.execute(
                "INSERT INTO scheduled_posts (id, profile_id, media_paths) VALUES (1, 1, ?)",
                ('{"not": "a list"}',),
            )
        with self.assertRaisesRegex(ValueError, "Invalid media_paths"):
            audit_media(self.root, self.db_url)

    def test_absent_db_must_not_be_created(self) -> None:
        missing = Path(self.tmp.name) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            audit_media(self.root, f"sqlite+aiosqlite:///{missing}")
        self.assertFalse(missing.exists())

    def test_non_sqlite_database_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "only file-based SQLite"):
            audit_media(self.root, "postgresql+asyncpg://host/db")

    def test_url_parser_supports_relative_and_absolute_sqlite_paths(self) -> None:
        self.assertEqual(
            _sqlite_path("sqlite+aiosqlite:///runtime/db.sqlite3"),
            Path("runtime/db.sqlite3").absolute(),
        )
        self.assertEqual(_sqlite_path(self.db_url), self.db_file)

    def test_json_cli_is_deterministic_and_does_not_modify_database(self) -> None:
        self._file("b", "pic", b"aaa")
        self._file("a", "pic", b"aaa")
        stdout = io.StringIO()
        before = self.db_file.stat().st_mtime_ns
        with contextlib.redirect_stdout(stdout):
            code = main(["--media-dir", str(self.root), "--db-url", self.db_url, "--json"])
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual([x["key"] for x in report["directories"]], ["a", "b"])
        self.assertEqual(report["orphan_candidates"], ["a", "b"])
        self.assertEqual(self.db_file.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
