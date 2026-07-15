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

logger = logging.getLogger(__name__)

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
        data = await up.read()
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
