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
    def is_done(self, publication_id: str, channel: str) -> bool: ...
    def mark_done(self, publication_id: str, channel: str, external_id: str | None) -> None: ...


class InMemoryIdempotencyStore:
    """Заглушка для тестов/фазы A. В проде — таблица в БД."""

    def __init__(self) -> None:
        self._done: dict[tuple[str, str], str | None] = {}

    def is_done(self, publication_id: str, channel: str) -> bool:
        return (publication_id, channel) in self._done

    def mark_done(self, publication_id: str, channel: str, external_id: str | None) -> None:
        self._done[(publication_id, channel)] = external_id


class JSONIdempotencyStore:
    """Файловый store для MVP-0 (вместо БД). Состояние (publication_id, channel) -> external_id.

    Достаточно для синхронного CLI. На MVP-1 заменяется БД-реализацией того же протокола.
    """

    def __init__(self, path) -> None:
        import json
        from pathlib import Path

        self._path = Path(path)
        self._json = json
        if self._path.exists():
            raw = self._json.loads(self._path.read_text("utf-8"))
            self._done = {tuple(k.split("|", 1)): v for k, v in raw.items()}
        else:
            self._done = {}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        raw = {f"{p}|{c}": v for (p, c), v in self._done.items()}
        self._path.write_text(self._json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")

    def is_done(self, publication_id: str, channel: str) -> bool:
        return (publication_id, channel) in self._done

    def mark_done(self, publication_id: str, channel: str, external_id: str | None) -> None:
        self._done[(publication_id, channel)] = external_id
        self._flush()
