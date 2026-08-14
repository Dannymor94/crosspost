"""ChannelTargetRepository — мультицели канала. Milestone «мультицели».

Пара (профиль, канал) может иметь НЕСКОЛЬКО именованных целей публикации.
Всё ВСЕГДА фильтруется по profile_id (изоляция, как в остальных репозиториях).

value_blob хранится шифртекстом (Fernet, тот же vault, что и credentials).
Расшифрованное значение живёт только в памяти вызывающего — не логируется,
не кешируется, не пишется на диск (CLAUDE.md §vault).

Ленивая миграция одиночной цели → первой цели списка (см. _seed_legacy):
существующие подключения не ломаются. Старое значение бралось из
  • CredentialKind.TARGET   — браузерные каналы (screen_name / org_id / channel_id);
  • telegram-блоб.target_channel — Telegram (цель внутри JSON-учётки сессии).
Данные НЕ мигрируются в DDL-хуке (там нет vault) — сид происходит здесь, при
первом чтении списка целей канала, когда целей ещё нет.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import ChannelTarget, Credential, CredentialKind
from crosspost.db.vault import decrypt_blob, encrypt_blob

# Имя первой цели, синтезированной из legacy-значения одиночной цели.
_SEED_NAME = "Основная"


class ChannelTargetRepository:
    """Async-репозиторий целей канала с изоляцией по profile_id."""

    def __init__(self, session: AsyncSession, *, vault: Fernet, profile_id: int) -> None:
        self._s = session
        self._vault = vault
        self._pid = profile_id

    # ── Чтение ────────────────────────────────────────────────────────────────

    async def list_targets(self, channel: str) -> list[ChannelTarget]:
        """Цели канала по порядку создания. Пусто → пытаемся засидить из legacy."""
        rows = await self._query(channel)
        if not rows:
            await self._seed_legacy(channel)
            rows = await self._query(channel)
        return rows

    async def get_target(self, target_id: int) -> ChannelTarget | None:
        """Цель по id (профиль-scoped). None если чужая/не найдена."""
        result = await self._s.execute(
            select(ChannelTarget).where(
                ChannelTarget.id == target_id,
                ChannelTarget.profile_id == self._pid,
            )
        )
        return result.scalar_one_or_none()

    def decrypt_value(self, target: ChannelTarget) -> str:
        """Расшифровать значение цели (screen_name / org_id / @канал). Только в памяти."""
        return decrypt_blob(self._vault, target.value_blob)

    # ── Запись ────────────────────────────────────────────────────────────────

    async def add_target(self, channel: str, name: str, value: str) -> ChannelTarget:
        """Создать цель. value шифруется; нормализацию делает вызывающий (роут)."""
        target = ChannelTarget(
            profile_id=self._pid,
            channel=channel,
            name=name,
            value_blob=encrypt_blob(self._vault, value),
        )
        self._s.add(target)
        await self._s.commit()
        await self._s.refresh(target)
        return target

    async def rename_target(self, target_id: int, name: str) -> bool:
        """Переименовать цель профиля. True если нашли."""
        result = await self._s.execute(
            sql_update(ChannelTarget)
            .where(ChannelTarget.id == target_id, ChannelTarget.profile_id == self._pid)
            .values(name=name)
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0

    async def set_value(self, target_id: int, value: str) -> bool:
        """Обновить значение цели (шифрует). True если нашли."""
        result = await self._s.execute(
            sql_update(ChannelTarget)
            .where(ChannelTarget.id == target_id, ChannelTarget.profile_id == self._pid)
            .values(value_blob=encrypt_blob(self._vault, value))
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0

    async def delete_target(self, target_id: int) -> bool:
        """Удалить цель профиля. True если что-то удалено. Идемпотентно."""
        result = await self._s.execute(
            delete(ChannelTarget).where(
                ChannelTarget.id == target_id,
                ChannelTarget.profile_id == self._pid,
            )
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0

    # ── Внутреннее ────────────────────────────────────────────────────────────

    async def _query(self, channel: str) -> list[ChannelTarget]:
        result = await self._s.execute(
            select(ChannelTarget)
            .where(ChannelTarget.profile_id == self._pid, ChannelTarget.channel == channel)
            .order_by(ChannelTarget.id)
        )
        return list(result.scalars().all())

    async def _seed_legacy(self, channel: str) -> None:
        """Синтезировать первую цель из legacy-одиночной цели, если она была.

        Значение сохраняется КАК ЕСТЬ (без нормализации) — нормализация происходит
        при использовании/показе (см. normalize_target), поэтому старое «грязное»
        значение (полный URL) не теряется и не роняет публикацию.
        """
        value = await self._legacy_credential(channel, CredentialKind.TARGET)
        if not value and channel == "telegram":
            blob = await self._legacy_credential("telegram", CredentialKind.API_TOKEN)
            if blob:
                try:
                    value = str(json.loads(blob).get("target_channel") or "")
                except (ValueError, TypeError):
                    value = ""
        if not value:
            return
        self._s.add(
            ChannelTarget(
                profile_id=self._pid,
                channel=channel,
                name=_SEED_NAME,
                value_blob=encrypt_blob(self._vault, value),
            )
        )
        await self._s.commit()

    async def _legacy_credential(self, channel: str, kind: CredentialKind) -> str:
        """Расшифрованный blob legacy-учётки (profile, channel, kind) или ""."""
        result = await self._s.execute(
            select(Credential.blob).where(
                Credential.profile_id == self._pid,
                Credential.channel == channel,
                Credential.kind == kind,
            )
        )
        row = result.first()
        return decrypt_blob(self._vault, row[0]) if row is not None else ""
