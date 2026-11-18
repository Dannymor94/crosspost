"""Read-only audit of publication media directories (T-213).

Run with ``python -m crosspost.media.diagnostics``.  This module never creates,
updates, or removes media or database rows.  An orphan is only a *candidate*:
hard-deleted scheduled rows and in-flight uploads have no durable media link.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


@dataclass(frozen=True)
class MediaDirectory:
    key: str
    size_bytes: int
    file_count: int
    referenced_by: tuple[str, ...]


@dataclass(frozen=True)
class MediaAudit:
    media_root: str
    total_bytes: int
    total_files: int
    root_file_bytes: int
    ignored_symlinks: int
    directories: tuple[MediaDirectory, ...]
    orphan_candidates: tuple[str, ...]
    missing_scheduled_directories: tuple[str, ...]
    unmatched_database_paths: tuple[str, ...]


def _sqlite_path(db_url: str) -> Path:
    """Parse standard relative/absolute SQLAlchemy SQLite URLs without opening a DB."""
    url = urlsplit(db_url)
    if url.scheme not in {"sqlite", "sqlite+aiosqlite"} or url.netloc or url.query:
        raise ValueError("Media audit supports only file-based SQLite DB_URL without query params")
    if not url.path.startswith("/"):
        raise ValueError("DB_URL must identify a SQLite file")
    # sqlite:///relative.db is relative; sqlite:////absolute.db is absolute.
    database = url.path[1:] if not url.path.startswith("//") else "/" + url.path.lstrip("/")
    if not database or database == ":memory:":
        raise ValueError("Media audit requires an existing on-disk database")
    return Path(unquote(database)).absolute()


def _media_key(path: str, root: Path) -> str | None:
    """Return the top-level directory containing a stored media file, if inside root."""
    candidate = Path(path).absolute()
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        return None
    return parts[0] if len(parts) >= 2 else None


def _db_references(
    db_url: str, root: Path
) -> tuple[dict[str, set[str]], set[str], tuple[str, ...]]:
    db_path = _sqlite_path(db_url)
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {db_path}")
    # SQLite mode=ro is intentional: the diagnostic must never create/migrate a DB.
    sqlite_uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
    conn = sqlite3.connect(sqlite_uri, uri=True)
    references: dict[str, set[str]] = defaultdict(set)
    scheduled_keys: set[str] = set()
    unmatched: list[str] = []
    try:
        conn.execute("BEGIN")  # consistent read snapshot across both tables
        for scheduled_id, profile_id, serialized in conn.execute(
            "SELECT id, profile_id, media_paths FROM scheduled_posts"
        ):
            paths = json.loads(serialized) if serialized is not None else []
            if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
                raise ValueError(f"Invalid media_paths in scheduled post {scheduled_id}")
            for path in paths:
                key = _media_key(path, root)
                if key is None:
                    unmatched.append(f"scheduled:{profile_id}/{scheduled_id}: {path}")
                    continue
                references[key].add(f"scheduled:{profile_id}/{scheduled_id}")
                scheduled_keys.add(key)
        for profile_id, publication_id in conn.execute(
            "SELECT DISTINCT profile_id, publication_id FROM publications"
        ):
            # For immediate web uploads the directory is named publication_id.
            # Scheduled rows use a different key ('sched-<uuid>'), so their
            # surviving publication rows cannot reconstruct a deleted link.
            references[str(publication_id)].add(f"publication:{profile_id}/{publication_id}")
    finally:
        conn.close()
    return references, scheduled_keys, tuple(sorted(unmatched))


def _walk_size(directory: Path) -> tuple[int, int, int]:
    """Count logical file sizes, never following symlinks out of MEDIA_DIR."""
    size = files = ignored_symlinks = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    ignored_symlinks += 1
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    size += entry.stat(follow_symlinks=False).st_size
                    files += 1
    return size, files, ignored_symlinks


def audit_media(root: Path, db_url: str) -> MediaAudit:
    """Inspect current filesystem and DB references without any writes."""
    root = root.absolute()
    if not root.is_dir() or root.is_symlink():
        raise FileNotFoundError(f"MEDIA_DIR is not a real directory: {root}")
    references, scheduled_keys, unmatched = _db_references(db_url, root)
    directories: list[MediaDirectory] = []
    total_bytes = total_files = ignored_symlinks = root_file_bytes = 0
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_symlink():
                ignored_symlinks += 1
            elif entry.is_dir(follow_symlinks=False):
                size, files, skipped = _walk_size(Path(entry.path))
                directories.append(
                    MediaDirectory(
                        key=entry.name,
                        size_bytes=size,
                        file_count=files,
                        referenced_by=tuple(sorted(references.get(entry.name, ()))),
                    )
                )
                total_bytes += size
                total_files += files
                ignored_symlinks += skipped
            elif entry.is_file(follow_symlinks=False):
                root_file_bytes += entry.stat(follow_symlinks=False).st_size
                total_files += 1
    total_bytes += root_file_bytes
    directories.sort(key=lambda item: (-item.size_bytes, item.key))
    present = {item.key for item in directories}
    return MediaAudit(
        media_root=str(root),
        total_bytes=total_bytes,
        total_files=total_files,
        root_file_bytes=root_file_bytes,
        ignored_symlinks=ignored_symlinks,
        directories=tuple(directories),
        orphan_candidates=tuple(item.key for item in directories if not item.referenced_by),
        missing_scheduled_directories=tuple(sorted(scheduled_keys - present)),
        unmatched_database_paths=unmatched,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only media storage audit (T-213)")
    parser.add_argument(
        "--media-dir", type=Path, default=Path(os.getenv("MEDIA_DIR", "runtime/media"))
    )
    parser.add_argument(
        "--db-url",
        default=os.getenv("DB_URL", "sqlite+aiosqlite:///runtime/db.sqlite3"),
    )
    parser.add_argument("--top", type=int, default=10, help="number of largest directories")
    parser.add_argument("--json", action="store_true", help="output the full report as JSON")
    args = parser.parse_args(argv)
    if args.top < 0:
        parser.error("--top must be non-negative")
    try:
        report = audit_media(args.media_dir, args.db_url)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"Media audit failed (nothing modified): {exc}\n")
    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    print(f"MEDIA_DIR: {report.media_root}")
    print(f"Total: {report.total_bytes} bytes, {report.total_files} files")
    print(
        f"Directories: {len(report.directories)}; "
        f"orphan candidates: {len(report.orphan_candidates)}"
    )
    print(
        f"Root files: {report.root_file_bytes} bytes; ignored symlinks: {report.ignored_symlinks}"
    )
    print("Largest directories:")
    for item in report.directories[: args.top]:
        refs = ", ".join(item.referenced_by) or "ORPHAN CANDIDATE"
        print(f"  {item.key}: {item.size_bytes} bytes; {item.file_count} files; {refs}")
    print(f"Orphan candidates: {', '.join(report.orphan_candidates) or 'none'}")
    missing = ", ".join(report.missing_scheduled_directories) or "none"
    print(f"Missing scheduled directories: {missing}")
    print(f"Unmatched stored paths: {len(report.unmatched_database_paths)}")
    print("WARNING: Orphans are candidates only; in-flight uploads and deleted rows are invisible.")
    print("No files or database rows were modified. Do not delete media based on this report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
