"""validate_connection() — точечная проверка живости подключения. Слой 0.4.

Читает учётку из vault через ProfileRepository, вызывает валидатор из реестра,
пишет результат в connections.state и диагностику в logs.

Вызывать при сохранении учётки или по требованию (UI "Проверить").
Фоновый health-check по крону — Слой 2, не сейчас.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from crosspost.channels.validators import VALIDATORS, ValidatorResult
from crosspost.db.models import ConnectionState, Log
from crosspost.db.profile_repo import ProfileRepository

logger = logging.getLogger(__name__)

_EXPIRES_SOON_DAYS = 7


def is_expires_soon(expires_at: datetime) -> bool:
    """True если токен истекает менее чем через EXPIRES_SOON_DAYS дней."""
    now = datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return (expires_at - now) < timedelta(days=_EXPIRES_SOON_DAYS)


async def validate_connection(
    repo: ProfileRepository,
    profile_id: int,
    channel: str,
    *,
    persist_expiry: bool = False,
) -> ConnectionState:
    """Проверить живость подключения и обновить connections.state.

    Алгоритм:
      1. Найти декларацию канала в реестре VALIDATORS.
      2. Получить учётку из vault (None если нет — только для browser, где
         storageState читается из файла, а не из blob).
      3. Вызвать validator.fn(credential).
      4. Записать результат в connections через upsert_connection.
      5. Записать диагностику в logs.

    persist_expiry=True → записать срок действия токена (expires_at) в подключение.
    Ставить ТОЛЬКО когда учётка только что сохранена/заменена (connect), НЕ при
    простой ре-валидации: иначе каждая проверка двигала бы «срок» на +60 дней и
    предупреждение «истекает» никогда не сработало бы (оценка, не точный срок).

    Возвращает новый ConnectionState (live | needs_relogin).
    Канал не в реестре → needs_relogin + запись в лог.
    """
    validator = VALIDATORS.get(channel)
    if validator is None:
        await _log(
            repo, profile_id, channel, "WARNING", f"Канал '{channel}' не в реестре валидаторов"
        )
        return await _write(repo, profile_id, channel, ConnectionState.NEEDS_RELOGIN)

    # Учётку берём из vault по КАНАЛУ СЕССИИ (per-profile). Для каналов, делящих
    # аккаунт (vk_channel → vk_wall), сессия лежит под session_channel.
    # Пусто → browser-валидатор получит пустой контекст → NEEDS_RELOGIN (не подхват чужой сессии).
    session_key = validator.session_channel or channel
    credential = await repo.get_credential(profile_id, session_key, validator.credential_kind)

    try:
        raw = await validator.fn(credential)
    except Exception as exc:
        logger.warning("validate_connection %s/%s exception: %s", profile_id, channel, exc)
        await _log(repo, profile_id, channel, "ERROR", f"Исключение при валидации: {exc}")
        return await _write(repo, profile_id, channel, ConnectionState.NEEDS_RELOGIN)

    # Coerce bool | ValidatorResult → (alive, expires_at)
    if isinstance(raw, ValidatorResult):
        alive = raw.alive
        expires_at = raw.expires_at
    else:
        alive = bool(raw)
        expires_at = None

    if alive:
        await _log(repo, profile_id, channel, "INFO", "Подключение активно")
        # expires_at записываем только при сохранении/замене токена (persist_expiry),
        # иначе ре-валидация сдвинула бы оценочный срок и предупреждение не сработало бы.
        return await _write(
            repo,
            profile_id,
            channel,
            ConnectionState.LIVE,
            expires_at=expires_at if persist_expiry else None,
        )
    else:
        await _log(repo, profile_id, channel, "WARNING", "Подключение требует ре-логина")
        return await _write(repo, profile_id, channel, ConnectionState.NEEDS_RELOGIN)


async def _write(
    repo: ProfileRepository,
    profile_id: int,
    channel: str,
    state: ConnectionState,
    *,
    expires_at=None,
) -> ConnectionState:
    await repo.upsert_connection(profile_id, channel, state, expires_at=expires_at)
    return state


async def _log(
    repo: ProfileRepository,
    profile_id: int,
    channel: str,
    level: str,
    message: str,
) -> None:
    log = Log(
        profile_id=profile_id,
        channel=channel,
        level=level,
        message=message,
    )
    repo._s.add(log)
    await repo._s.commit()
