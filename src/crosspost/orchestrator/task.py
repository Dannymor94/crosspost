"""Состояние ПУБЛИКАЦИИ (не подключения). Эпик 0/3.

Идемпотентность — по внутреннему publication_id, сгенерированному ДО отправки.
Источник истины — (publication_id, channel). external_id тут НЕ участвует.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class TaskStatus(StrEnum):
    NEW = "new"
    QUEUED = "queued"
    ATTEMPTING = "attempting"
    DONE = "done"
    FAILED = "failed"
    HELD = "held"  # уперлась в мёртвое подключение / открытый breaker


@dataclass
class Task:
    publication_id: str
    channel: str
    status: TaskStatus = TaskStatus.NEW
    attempt_count: int = 0
    external_id: str | None = None  # квитанция после успеха
    error: str | None = None


def new_publication_id() -> str:
    """Внутренний идемпотентный ключ. Один на отправку, общий для всех каналов."""
    return uuid.uuid4().hex


class IdempotencyStore(Protocol):
    """Async-протокол: реализации пишут через async-движок БД (или in-memory/файл).

    Все методы — корутины: единственный бэкенд в проде (CheckpointStore) живёт на
    том же async SQLAlchemy-движке, что и остальной стейт, без второго соединения.
    """

    async def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool: ...
    async def mark_done(
        self, publication_id: str, channel: str, external_id: str | None, *, target: str = ""
    ) -> None: ...
    async def clear_done(self, publication_id: str, channel: str, *, target: str = "") -> None:
        """Remove an entry so the next attempt treats it as fresh (not done).

        Used to discard a dead checkpoint (e.g. an Instagram container that reached
        terminal ERROR/EXPIRED) so the next retry creates a new one instead of
        re-reading the dead container id.
        """
        ...

    async def get_external_id(
        self, publication_id: str, channel: str, *, target: str = ""
    ) -> str | None:
        """Квитанция по ключу (publication_id, channel, target), либо None.

        target — измерение цели (мультицели): одна площадка шлёт в несколько целей
        под одним publication_id, каждая дедупится отдельно. Пусто "" — таргетлесс
        (Telegraph/legacy). Нужна адаптерам с многошаговой отправкой: при ретрае
        после падения второго вызова позволяет прочитать id уже отправленного первого
        сообщения, не пересылая его повторно (см. TelegramAdapter, двухсообщённый
        режим). external_id — квитанция, НЕ ключ дедупа (§6.1).
        """
        ...

    async def delete_checkpoints(self, publication_id: str) -> None:
        """Удалить шаг-ключи "<publication_id>#<step>" после финализации публикации.

        Терминальную строку (чистый publication_id без '#') не трогает.
        """
        ...


def _is_step_key(key_pub: str, publication_id: str) -> bool:
    """True если key_pub — шаг-ключ вида "<publication_id>#<step>"."""
    return key_pub.startswith(f"{publication_id}#")


class InMemoryIdempotencyStore:
    """Заглушка для тестов/фазы A. В проде — таблица в БД (async)."""

    def __init__(self) -> None:
        self._done: dict[tuple[str, str, str], str | None] = {}

    async def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool:
        return (publication_id, channel, target) in self._done

    async def mark_done(
        self, publication_id: str, channel: str, external_id: str | None, *, target: str = ""
    ) -> None:
        self._done[(publication_id, channel, target)] = external_id

    async def clear_done(self, publication_id: str, channel: str, *, target: str = "") -> None:
        self._done.pop((publication_id, channel, target), None)

    async def get_external_id(
        self, publication_id: str, channel: str, *, target: str = ""
    ) -> str | None:
        return self._done.get((publication_id, channel, target))

    async def delete_checkpoints(self, publication_id: str) -> None:
        for key in [k for k in self._done if _is_step_key(k[0], publication_id)]:
            self._done.pop(key, None)


class JSONIdempotencyStore:
    """Файловый store для MVP-0 (вместо БД). Ключ (publication_id, channel, target).

    Async-протокол (методы — корутины), но I/O синхронный (файл). На MVP-1 заменяется
    БД-реализацией того же протокола. Старые 2-частные файлы (без цели) читаются с
    target="" (обратная совместимость).
    """

    def __init__(self, path) -> None:
        import json
        from pathlib import Path

        self._path = Path(path)
        self._json = json
        if self._path.exists():
            raw = self._json.loads(self._path.read_text("utf-8"))
            self._done = {self._parse_key(k): v for k, v in raw.items()}
        else:
            self._done = {}

    @staticmethod
    def _parse_key(k: str) -> tuple[str, str, str]:
        parts = k.split("|", 2)
        while len(parts) < 3:  # старый формат "pub|channel" → цель ""
            parts.append("")
        return (parts[0], parts[1], parts[2])

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        raw = {f"{p}|{c}|{t}": v for (p, c, t), v in self._done.items()}
        self._path.write_text(self._json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")

    async def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool:
        return (publication_id, channel, target) in self._done

    async def mark_done(
        self, publication_id: str, channel: str, external_id: str | None, *, target: str = ""
    ) -> None:
        self._done[(publication_id, channel, target)] = external_id
        self._flush()

    async def clear_done(self, publication_id: str, channel: str, *, target: str = "") -> None:
        self._done.pop((publication_id, channel, target), None)
        self._flush()

    async def get_external_id(
        self, publication_id: str, channel: str, *, target: str = ""
    ) -> str | None:
        return self._done.get((publication_id, channel, target))

    async def delete_checkpoints(self, publication_id: str) -> None:
        removed = [k for k in self._done if _is_step_key(k[0], publication_id)]
        for key in removed:
            self._done.pop(key, None)
        if removed:
            self._flush()
