"""Периодический health-check подключений (T-202).

Фоновый цикл проверяет все LIVE-подключения по всем профилям,
используя те же валидирующие функции, что и validate_connection.

Отличие от validate_connection (разовая ре-валидация):
  • исключение из validator.fn = временный сбой → состояние сохраняется;
  • BANNED → никогда не трогаем;
  • NEEDS_RELOGIN → пропускаем (уже помечено, ждёт действия пользователя);
  • confirmed_dead=False из ValidatorResult → временный сбой → состояние сохраняется;
  • confirmed_dead=True → атомарный CAS (set_needs_relogin_if_live) → не перезапишет BANNED;
  • каналы с одинаковым session_key проверяются ОДНИМ запросом (vk_wall + vk_channel → 1 проба).

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
            except Exception as exc:  # noqa: BLE001
                logger.warning("health check tick failed (%s)", type(exc).__name__)
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
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "health check failed for profile %s (%s)", profile.id, type(exc).__name__
                )

    async def _check_profile(self, repo: ProfileRepository, profile_id: int) -> None:
        """Сгруппировать LIVE-каналы по session_key, проверить каждую группу один раз."""
        connections = await repo.get_connections(profile_id)

        # Group LIVE channels by session_key to avoid redundant probes (e.g. vk_wall + vk_channel).
        # ponytail: dict preserves insertion order → deterministic probe order
        session_groups: dict[str, list[str]] = {}
        for conn in connections:
            if conn.state != ConnectionState.LIVE:
                continue
            validator = VALIDATORS.get(conn.channel)
            if validator is None or not validator.enabled:
                continue
            sk = validator.session_channel or conn.channel
            session_groups.setdefault(sk, []).append(conn.channel)

        for session_key, channels in session_groups.items():
            try:
                await self._check_session(repo, profile_id, session_key, channels)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "health check failed for profile %s session_key %s (%s)",
                    profile_id,
                    session_key,
                    type(exc).__name__,
                )

    async def _check_session(
        self,
        repo: ProfileRepository,
        profile_id: int,
        session_key: str,
        affected_channels: list[str],
    ) -> None:
        """Probe session_key once; apply confirmed-dead result to all affected_channels.

        Uses the session_key channel's validator (primary probe owner).
        Falls back to the first affected channel's validator if session_key has no entry.
        """
        primary_validator = VALIDATORS.get(session_key)
        if primary_validator is None:
            primary_validator = VALIDATORS.get(affected_channels[0])
        if primary_validator is None or not primary_validator.enabled:
            return

        # Single consistent read: plaintext for the probe, raw blob as CAS fence
        credential, credential_blob = await repo.get_credential_with_blob(
            profile_id, session_key, primary_validator.credential_kind
        )

        try:
            raw = await primary_validator.fn(credential)
        except Exception as exc:
            # Transient error — preserve state; log safe category (never log str(exc))
            exc_type = type(exc).__name__
            logger.warning(
                "health check %s/%s: transient error (%s), state preserved",
                profile_id,
                session_key,
                exc_type,
            )
            for ch in affected_channels:
                repo._s.add(
                    Log(
                        profile_id=profile_id,
                        channel=ch,
                        level="INFO",
                        message=f"health check: transient error ({exc_type}), state preserved",
                    )
                )
            await repo._s.commit()
            return

        alive = raw.alive if isinstance(raw, ValidatorResult) else bool(raw)
        confirmed_dead = (
            (isinstance(raw, ValidatorResult) and raw.confirmed_dead) if not alive else False
        )

        if not alive and not confirmed_dead:
            # Transient/unknown result — preserve state, write diagnostic log
            logger.info(
                "health check %s/%s: transient/unknown result, state preserved",
                profile_id,
                session_key,
            )
            for ch in affected_channels:
                repo._s.add(
                    Log(
                        profile_id=profile_id,
                        channel=ch,
                        level="INFO",
                        message="health check: transient/unknown result, state preserved",
                    )
                )
            await repo._s.commit()
            return

        if confirmed_dead:
            logger.warning("health check: %s/%s → confirmed auth failure", profile_id, session_key)
            for ch in affected_channels:
                # CAS: only update if still LIVE — won't overwrite BANNED or a concurrent relogin
                applied = await repo.set_needs_relogin_if_live(
                    profile_id,
                    ch,
                    session_key=session_key,
                    credential_kind=primary_validator.credential_kind,
                    credential_blob_snapshot=credential_blob,
                )
                if applied:
                    repo._s.add(
                        Log(
                            profile_id=profile_id,
                            channel=ch,
                            level="WARNING",
                            message="health check: confirmed auth failure → needs_relogin",
                        )
                    )
            await repo._s.commit()
