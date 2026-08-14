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
    def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool: ...
    def mark_done(
        self, publication_id: str, channel: str, external_id: str | None, *, target: str = ""
    ) -> None: ...
    def get_external_id(self, publication_id: str, channel: str, *, target: str = "") -> str | None:
        """Квитанция по ключу (publication_id, channel, target), либо None.

        target — измерение цели (мультицели): одна площадка шлёт в несколько целей
        под одним publication_id, каждая дедупится отдельно. Пусто "" — таргетлесс
        (Telegraph/legacy). Нужна адаптерам с многошаговой отправкой: при ретрае
        после падения второго вызова позволяет прочитать id уже отправленного первого
        сообщения, не пересылая его повторно (см. TelegramAdapter, двухсообщённый
        режим). external_id — квитанция, НЕ ключ дедупа (§6.1).
        """
        ...


class InMemoryIdempotencyStore:
    """Заглушка для тестов/фазы A. В проде — таблица в БД."""

    def __init__(self) -> None:
        self._done: dict[tuple[str, str, str], str | None] = {}

    def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool:
        return (publication_id, channel, target) in self._done

    def mark_done(
        self, publication_id: str, channel: str, external_id: str | None, *, target: str = ""
    ) -> None:
        self._done[(publication_id, channel, target)] = external_id

    def get_external_id(self, publication_id: str, channel: str, *, target: str = "") -> str | None:
        return self._done.get((publication_id, channel, target))


class JSONIdempotencyStore:
    """Файловый store для MVP-0 (вместо БД). Ключ (publication_id, channel, target).

    Достаточно для синхронного CLI. На MVP-1 заменяется БД-реализацией того же протокола.
    Старые 2-частные файлы (без цели) читаются с target="" (обратная совместимость).
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

    def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool:
        return (publication_id, channel, target) in self._done

    def mark_done(
        self, publication_id: str, channel: str, external_id: str | None, *, target: str = ""
    ) -> None:
        self._done[(publication_id, channel, target)] = external_id
        self._flush()

    def get_external_id(self, publication_id: str, channel: str, *, target: str = "") -> str | None:
        return self._done.get((publication_id, channel, target))
