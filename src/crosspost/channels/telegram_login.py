"""Интерактивный вход в Telegram прямо из UI. Итерация 1 / Слой 1.

Многошаговый флоу (api_id/api_hash/канал → телефон → код → [2FA-пароль] → StringSession):

    begin(profile_id, api_id, api_hash, target_channel)  # создать клиент, connect
    request_code(profile_id, phone)                       # send_code_request
    submit_code(profile_id, code[, password])             # sign_in [+ 2FA]

Состояние флоу держится НА СЕРВЕРЕ в памяти (_LOGIN_SESSIONS), не в браузере.
После успеха отдаётся только StringSession + конфиг канала — телефон и пароль
НЕ сохраняются (живут в памяти на время флоу и стираются на finalize).

Учётка площадки (api_id/api_hash/target_channel/session) сериализуется в JSON и
шифруется vault'ом на уровне репозитория — этот модуль сам в БД не пишет.

Изоляция: у UI-флоу своя StringSession, привязанная к profile_id. CLI-сессия
(runtime/sessions/telegram.session) здесь НЕ используется.

Тестируется на моках клиента Telethon: подменяется _build_client.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)

logger = logging.getLogger(__name__)


class TelegramLoginError(Exception):
    """Ошибка флоу с человекочитаемым сообщением (показывается в UI)."""


@dataclass
class SignInResult:
    """Результат шага sign_in.

    step="password" — нужен облачный пароль (2FA), вход ещё не завершён.
    step="done"     — вход завершён, session_string готов к сохранению.
    """

    step: str
    session_string: str | None = None
    api_id: int | None = None
    api_hash: str | None = None
    target_channel: str | None = None


@dataclass
class TelegramLoginSession:
    """Живое состояние одного флоу входа. Хранится в памяти сервера."""

    profile_id: int
    api_id: int
    api_hash: str
    target_channel: str
    client: Any  # telethon.TelegramClient (или мок в тестах)
    phone: str | None = None
    phone_code_hash: str | None = None
    awaiting_password: bool = False


# profile_id → активный флоу. Один вход на профиль в момент времени.
_LOGIN_SESSIONS: dict[int, TelegramLoginSession] = {}


def _build_client(api_id: int, api_hash: str) -> Any:
    """Создать пустой Telethon-клиент со StringSession. Подменяется в тестах."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(), api_id, api_hash)


def _build_client_from_session(api_id: int, api_hash: str, session: str) -> Any:
    """Клиент из ГОТОВОЙ StringSession (для импорта в обход входа по коду). Патчится в тестах."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session), api_id, api_hash)


def _get_session(profile_id: int) -> TelegramLoginSession:
    sess = _LOGIN_SESSIONS.get(profile_id)
    if sess is None:
        raise TelegramLoginError(
            "Флоу входа не начат или устарел — начните заново с ввода api_id/api_hash."
        )
    return sess


async def _discard(profile_id: int) -> None:
    """Закрыть и удалить активный флоу профиля, если он есть."""
    sess = _LOGIN_SESSIONS.pop(profile_id, None)
    if sess is not None:
        try:
            await sess.client.disconnect()
        except Exception as exc:  # noqa: BLE001 — best-effort очистка
            logger.debug("disconnect on discard failed: %s", exc)


async def begin(
    profile_id: int,
    api_id: int,
    api_hash: str,
    target_channel: str,
) -> str:
    """Шаг 1: создать клиент, подключиться, запомнить конфиг. Возвращает следующий шаг."""
    if not api_id or not api_hash:
        raise TelegramLoginError(
            "Укажите api_id и api_hash (страница «App configuration» на my.telegram.org/apps)."
        )
    if not target_channel:
        raise TelegramLoginError("Укажите канал назначения (например, @mychannel).")

    # Сбросить возможный недоделанный предыдущий флоу этого профиля.
    await _discard(profile_id)

    client = _build_client(api_id, api_hash)
    try:
        await client.connect()
    except Exception as exc:
        raise TelegramLoginError(f"Не удалось подключиться к Telegram: {exc}") from exc

    _LOGIN_SESSIONS[profile_id] = TelegramLoginSession(
        profile_id=profile_id,
        api_id=api_id,
        api_hash=api_hash,
        target_channel=target_channel,
        client=client,
    )
    return "phone"


async def request_code(profile_id: int, phone: str) -> str:
    """Шаг 2: отправить код на телефон. Возвращает следующий шаг ('code')."""
    if not phone:
        raise TelegramLoginError("Укажите номер телефона в международном формате (+7…).")
    sess = _get_session(profile_id)
    try:
        sent = await sess.client.send_code_request(phone)
        # Диагностика: КАКИМ способом Telegram отправил код (app/sms/call) и что
        # предложит следующим. Помогает, если код «не приходит» (в приложение vs SMS).
        logger.info(
            "telegram send_code type=%s next_type=%s",
            type(getattr(sent, "type", None)).__name__,
            type(getattr(sent, "next_type", None)).__name__,
        )
    except PhoneNumberInvalidError as exc:
        raise TelegramLoginError("Неверный номер телефона.") from exc
    except ApiIdInvalidError as exc:
        raise TelegramLoginError("Неверные api_id / api_hash.") from exc
    except FloodWaitError as exc:
        raise TelegramLoginError(
            f"Слишком много попыток. Повторите через {exc.seconds} с."
        ) from exc
    except SendCodeUnavailableError as exc:
        # Telegram уже отправил код и отказывается слать ещё один этому номеру.
        # Значит первый код уже ушёл (в приложение) — просить его заново бесполезно.
        raise TelegramLoginError(
            "Telegram уже отправил код и пока не присылает новый для этого номера. "
            "Проверьте служебный чат «Telegram» в приложении — код (5 цифр) должен быть "
            "там. Если его нет, закройте окно и начните вход заново через несколько минут."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — не отдаём 500, показываем причину пользователю
        logger.warning("telegram send_code failed: %s", exc)
        raise TelegramLoginError(
            f"Не удалось отправить код: {exc}. Попробуйте начать вход заново чуть позже."
        ) from exc

    sess.phone = phone
    sess.phone_code_hash = getattr(sent, "phone_code_hash", None)
    sess.awaiting_password = False
    return "code"


async def submit_code(
    profile_id: int,
    code: str | None = None,
    password: str | None = None,
) -> SignInResult:
    """Шаг 3: подтвердить код (и, если включён, облачный пароль 2FA).

    Возвращает SignInResult(step="password") если нужен 2FA-пароль;
    SignInResult(step="done", session_string=…) при успехе.
    """
    sess = _get_session(profile_id)

    # Ввод кода — пропускаем, если уже прошли его и ждём только пароль.
    if not sess.awaiting_password:
        if not code:
            raise TelegramLoginError("Введите код из Telegram.")
        try:
            await sess.client.sign_in(
                phone=sess.phone,
                code=code,
                phone_code_hash=sess.phone_code_hash,
            )
        except SessionPasswordNeededError:
            sess.awaiting_password = True
        except (PhoneCodeInvalidError, PhoneCodeEmptyError) as exc:
            raise TelegramLoginError("Неверный код из Telegram.") from exc
        except PhoneCodeExpiredError as exc:
            raise TelegramLoginError("Код устарел — запросите новый.") from exc
        except FloodWaitError as exc:
            raise TelegramLoginError(
                f"Слишком много попыток. Повторите через {exc.seconds} с."
            ) from exc
        except TelegramLoginError:
            raise
        except Exception as exc:  # noqa: BLE001 — не 500, показываем причину
            logger.warning("telegram sign_in(code) failed: %s", exc)
            raise TelegramLoginError(f"Не удалось подтвердить код: {exc}") from exc

    # Шаг 2FA (если Telegram потребовал облачный пароль).
    if sess.awaiting_password:
        if not password:
            return SignInResult(step="password")
        try:
            await sess.client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise TelegramLoginError("Неверный облачный пароль (2FA).") from exc
        except Exception as exc:  # noqa: BLE001 — не 500, показываем причину
            logger.warning("telegram sign_in(2fa) failed: %s", exc)
            raise TelegramLoginError(f"Не удалось подтвердить пароль: {exc}") from exc

    return await _finalize(sess)


async def import_session(
    profile_id: int,
    *,
    api_id: int,
    api_hash: str,
    target_channel: str,
    session: str,
) -> str:
    """Импорт готовой StringSession в обход входа по коду.

    Проверяет сессию через get_me() и возвращает валидную строку. Учётку в БД
    пишет РОУТ (как и в обычном флоу). Полезно при флуд-лимите Telegram.
    """
    if not api_id or not api_hash:
        raise TelegramLoginError("Укажите api_id и api_hash.")
    if not target_channel:
        raise TelegramLoginError("Укажите канал назначения (например, @mychannel).")
    session = (session or "").strip()
    if not session:
        raise TelegramLoginError("Вставьте строку сессии (StringSession).")

    # Закрыть возможный недоделанный флоу входа по коду этого профиля.
    await _discard(profile_id)

    client = _build_client_from_session(api_id, api_hash, session)
    try:
        await client.connect()
        me = await client.get_me()
    except Exception as exc:  # noqa: BLE001 — показываем причину, не 500
        raise TelegramLoginError(
            f"Не удалось проверить сессию: {exc}. Проверьте строку, api_id и api_hash."
        ) from exc
    finally:
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.debug("disconnect after import failed: %s", exc)

    if me is None:
        raise TelegramLoginError(
            "Сессия недействительна или не авторизована (get_me вернул пусто)."
        )
    return session


async def _finalize(sess: TelegramLoginSession) -> SignInResult:
    """Снять StringSession, закрыть клиент, стереть флоу (телефон/пароль не сохраняем)."""
    session_string = sess.client.session.save()
    api_id, api_hash, target_channel = sess.api_id, sess.api_hash, sess.target_channel
    await _discard(sess.profile_id)
    return SignInResult(
        step="done",
        session_string=session_string,
        api_id=api_id,
        api_hash=api_hash,
        target_channel=target_channel,
    )


# ── Сериализация учётки для vault ─────────────────────────────────────────────


def build_credential_blob(*, api_id: int, api_hash: str, target_channel: str, session: str) -> str:
    """Собрать JSON-учётку Telegram для шифрования vault'ом."""
    return json.dumps(
        {
            "api_id": api_id,
            "api_hash": api_hash,
            "target_channel": target_channel,
            "session": session,
        }
    )


def parse_credential_blob(blob: str) -> dict[str, Any]:
    """Разобрать JSON-учётку Telegram. Пустой dict, если формат не распознан."""
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
