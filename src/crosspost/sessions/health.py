"""Периодический health-check подключений (T-202).

Фоновый цикл проверяет все LIVE-подключения по всем профилям,
используя те же валидирующие функции, что и validate_connection.

Отличие от validate_connection (разовая ре-валидация):
  • исключение из validator.fn = временный сбой → состояние сохраняется;
  • BANNED → никогда не трогаем;
  • NEEDS_RELOGIN → пропускаем (уже помечено, ждёт действия пользователя).

Запускается как asyncio.Task в lifespan приложения.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.channels.validators import VALIDATORS, ValidatorResult
from crosspost.db.models import ConnectionState, Log
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import get_vault

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]


class HealthChecker:
    """Периодически проверяет LIVE-подключения и переводит протухшие в needs_relogin."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_seconds: float = 300.0,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._factory = session_factory
        self._poll = max(0.0, poll_seconds)
        self._sleep = sleep

    @classmethod
    def from_env(cls, session_factory: async_sessionmaker[AsyncSession]) -> HealthChecker:
        return cls(
            session_factory,
            poll_seconds=float(os.environ.get("HEALTH_CHECK_POLL_SECONDS", "300")),
        )

    async def run_forever(self) -> None:
        logger.info("health checker started (poll=%ss)", self._poll)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("health checker stopped")
                raise
            except Exception:  # noqa: BLE001
                logger.exception("health check tick failed")
            await self._sleep(self._poll)

    async def run_once(self) -> None:
        """Один проход: проверить все LIVE-подключения всех профилей."""
        async with self._factory() as session:
            repo = ProfileRepository(session, vault=get_vault())
            profiles = await repo.list_profiles()

        for profile in profiles:
            try:
                async with self._factory() as session:
                    repo = ProfileRepository(session, vault=get_vault())
                    await self._check_profile(repo, profile.id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("health check failed for profile %s", profile.id)

    async def _check_profile(self, repo: ProfileRepository, profile_id: int) -> None:
        connections = await repo.get_connections(profile_id)
        for conn in connections:
            if conn.state != ConnectionState.LIVE:
                continue  # BANNED and NEEDS_RELOGIN are skipped
            try:
                await self._check_one(repo, profile_id, conn.channel)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception(
                    "health check failed for profile %s channel %s", profile_id, conn.channel
                )

    async def _check_one(self, repo: ProfileRepository, profile_id: int, channel: str) -> None:
        validator = VALIDATORS.get(channel)
        if validator is None or not validator.enabled:
            return

        session_key = validator.session_channel or channel
        credential = await repo.get_credential(profile_id, session_key, validator.credential_kind)

        try:
            raw = await validator.fn(credential)
        except Exception as exc:
            # Transient error (network, timeout, proxy) — preserve current state
            logger.warning(
                "health check %s/%s: transient error, state preserved: %s",
                profile_id,
                channel,
                exc,
            )
            return

        alive = raw.alive if isinstance(raw, ValidatorResult) else bool(raw)
        if not alive:
            logger.warning("health check: %s/%s → needs_relogin", profile_id, channel)
            await repo.upsert_connection(profile_id, channel, ConnectionState.NEEDS_RELOGIN)
            log = Log(
                profile_id=profile_id,
                channel=channel,
                level="WARNING",
                message="health check: подключение недействительно → needs_relogin",
            )
            repo._s.add(log)
            await repo._s.commit()
