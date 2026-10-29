"""Временное хранилище медиа для публикаций. Итерация 2а.

Файлы кладём под runtime/media/<key>/ (key = publication_id или sched-<id>).
Чистим, когда ВСЕ каналы публикации финализировались (или при отмене scheduled).
Корень — из env MEDIA_DIR (по умолчанию runtime/media), не коммитится.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from crosspost.content.media_kind import MediaKind, detect_media_kind

logger = logging.getLogger(__name__)

_IMAGE_MAX_BYTES = 25 * 1024 * 1024  # 25 MB
_CHUNK = 256 * 1024  # 256 KB read chunks


def _video_max_bytes() -> int:
    return int(os.environ.get("VIDEO_MAX_BYTES", str(500 * 1024 * 1024)))


class MediaUploadError(ValueError):
    """Uploaded file exceeds the allowed size cap."""


_MEDIA_ROOT_DEFAULT = "runtime/media"


def _media_root() -> Path:
    return Path(os.environ.get("MEDIA_DIR", _MEDIA_ROOT_DEFAULT))


def media_dir(key: str) -> Path:
    return _media_root() / key


async def save_uploads(files, key: str) -> list[str]:
    """Сохранить загруженные файлы в media_dir(key). Вернуть список путей (str)."""
    target = media_dir(key)
    target.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for i, up in enumerate(files):
        name = Path(up.filename or f"file{i}").name  # только имя, без каталогов
        dest = target / f"{i:02d}_{name}"
        is_video = detect_media_kind(name) == MediaKind.VIDEO
        cap = _video_max_bytes() if is_video else _IMAGE_MAX_BYTES
        label = "видео" if is_video else "изображения"
        cap_mb = cap // (1024 * 1024)
        # ponytail: SpooledTemporaryFile is sync; chunked copy avoids double-buffer
        total = 0
        up.file.seek(0)
        with open(dest, "wb") as out:
            while True:
                chunk = up.file.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise MediaUploadError(
                        f"Размер {label} превышает допустимый лимит ({cap_mb} МБ)."
                    )
                out.write(chunk)
        paths.append(str(dest))
    return paths


def save_binaries(items: list[tuple[str, bytes]], key: str) -> list[str]:
    """Сохранить уже готовые байты (name, data) в media_dir(key). Вернуть пути.

    Для файлов, скачанных из внешних источников (папка/http/Drive/Диск) — им не
    нужен UploadFile-обёртка, у нас уже есть байты и имя.
    """
    target = media_dir(key)
    target.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for i, (name, data) in enumerate(items):
        safe = Path(name or f"file{i}").name  # только имя, без каталогов
        dest = target / f"{i:02d}_{safe}"
        dest.write_bytes(data)
        paths.append(str(dest))
    return paths


def cleanup_media(key: str) -> None:
    """Удалить каталог медиа publication'а/scheduled. Идемпотентно."""
    d = media_dir(key)
    if d.exists():
        try:
            shutil.rmtree(d)
        except OSError as exc:
            logger.warning("cleanup media %s failed: %s", key, exc)
