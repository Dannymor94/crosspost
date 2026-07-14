"""ProfileRepository — единственный вход к данным профиля. Слой 0.3.

Жёсткая изоляция: КАЖДЫЙ метод принимает profile_id и фильтрует по нему.
Нет методов, возвращающих данные без явного profile_id.

Credentials хранятся как шифртекст (vault из слоя 0.2). Расшифрованное значение
живёт только в памяти: не логируется, не кешируется, не записывается на диск.

Пример:
    repo = ProfileRepository(session, vault=get_vault())
    profile = await repo.create_profile("alice")
    await repo.set_credential(profile.id, "telegram", CredentialKind.API_TOKEN, "secret")
    token = await repo.get_credential(profile.id, "telegram", CredentialKind.API_TOKEN)
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import (
    Connection,
    ConnectionState,
    Credential,
    CredentialKind,
    Profile,
)
from crosspost.db.vault import decrypt_blob, encrypt_blob


class ProfileRepository:
    """Async-репозиторий профилей с жёсткой изоляцией по profile_id."""

    def __init__(self, session: AsyncSession, *, vault: Fernet) -> None:
        self._s = session
        self._vault = vault

    # ── Profiles ─────────────────────────────────────────────────────────────

    async def create_profile(self, name: str) -> Profile:
        """Создать профиль с уникальным именем. Бросает IntegrityError при дублировании."""
        profile = Profile(name=name)
        self._s.add(profile)
        await self._s.commit()
        await self._s.refresh(profile)
        return profile

    async def list_profiles(self) -> list[Profile]:
        """Вернуть все профили (системный список, не фильтруется по profile_id)."""
        result = await self._s.execute(select(Profile).order_by(Profile.id))
        return list(result.scalars().all())

    async def get_profile(self, profile_id: int) -> Profile | None:
        """Вернуть профиль по id или None."""
        return await self._s.get(Profile, profile_id)

    # ── Connections ───────────────────────────────────────────────────────────

    async def get_connections(self, profile_id: int) -> list[Connection]:
        """Вернуть все подключения профиля."""
        result = await self._s.execute(
            select(Connection)
            .where(Connection.profile_id == profile_id)
            .order_by(Connection.channel)
        )
        return list(result.scalars().all())

    async def get_connection(self, profile_id: int, channel: str) -> Connection | None:
        """Вернуть подключение (profile_id, channel) или None."""
        result = await self._s.execute(
            select(Connection).where(
                Connection.profile_id == profile_id,
                Connection.channel == channel,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_connection(
        self, profile_id: int, channel: str, state: ConnectionState
    ) -> Connection:
        """Создать или обновить состояние подключения (profile_id × channel)."""
        stmt = (
            sqlite_insert(Connection)
            .values(profile_id=profile_id, channel=channel, state=state)
            .on_conflict_do_update(
                index_elements=["profile_id", "channel"],
                set_={"state": state},
            )
        )
        await self._s.execute(stmt)
        await self._s.commit()
        conn = await self.get_connection(profile_id, channel)
        assert conn is not None
        return conn

    # ── Credentials ───────────────────────────────────────────────────────────

    async def set_credential(
        self,
        profile_id: int,
        channel: str,
        kind: CredentialKind,
        plaintext: str,
    ) -> None:
        """Зашифровать plaintext и сохранить blob (upsert).

        plaintext НЕ логируется и НЕ хранится в атрибутах объекта.
        blob в БД — всегда шифртекст.
        """
        blob = encrypt_blob(self._vault, plaintext)
        stmt = (
            sqlite_insert(Credential)
            .values(profile_id=profile_id, channel=channel, kind=kind, blob=blob)
            .on_conflict_do_update(
                index_elements=["profile_id", "channel", "kind"],
                set_={"blob": blob},
            )
        )
        await self._s.execute(stmt)
        await self._s.commit()

    async def get_credential(
        self,
        profile_id: int,
        channel: str,
        kind: CredentialKind,
    ) -> str | None:
        """Расшифровать и вернуть plaintext. None если запись не найдена.

        Расшифрованная строка живёт только в памяти вызывающего кода.
        """
        result = await self._s.execute(
            select(Credential.blob).where(
                Credential.profile_id == profile_id,
                Credential.channel == channel,
                Credential.kind == kind,
            )
        )
        row = result.first()
        if row is None:
            return None
        return decrypt_blob(self._vault, row[0])

    async def delete_credential(
        self,
        profile_id: int,
        channel: str,
        kind: CredentialKind,
    ) -> None:
        """Удалить учётку (profile_id, channel, kind). Идемпотентно (нет — no-op)."""
        await self._s.execute(
            delete(Credential).where(
                Credential.profile_id == profile_id,
                Credential.channel == channel,
                Credential.kind == kind,
            )
        )
        await self._s.commit()

    async def delete_connection(self, profile_id: int, channel: str) -> None:
        """Удалить строку подключения (profile_id, channel) → канал станет «не подключён».

        «Не подключён» = ОТСУТСТВИЕ строки (в enum такого состояния нет). Идемпотентно.
        """
        await self._s.execute(
            delete(Connection).where(
                Connection.profile_id == profile_id,
                Connection.channel == channel,
            )
        )
        await self._s.commit()
