"""tests/test_tg_session_command.py — CLI tg-session: интерактивный вход → StringSession."""

from __future__ import annotations

import os
from io import StringIO
from unittest.mock import patch

import pytest


class _FakeSession:
    def save(self) -> str:
        return "1BVtsOKFAKESESSIONSTRING999"


class _FakeClient:
    """Мок TelegramClient: start() без реального подключения, session.save() → строка."""

    def __init__(self, *args, **kwargs) -> None:
        self.session = _FakeSession()

    async def start(self, *, phone, code_callback, password) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def _patch_telethon(client_cls):
    """Патчим TelegramClient там, где он реально импортируется внутри _run_tg_session."""
    import telethon

    return patch.object(telethon, "TelegramClient", client_cls)


def _run(argv: list[str]) -> tuple[int, str]:
    """Вызвать main() (который делает asyncio.run() сам) с перехватом stdout."""
    from crosspost.__main__ import main

    buf = StringIO()
    with patch("sys.stdout", buf):
        code = main(argv)
    return code, buf.getvalue()


def test_tg_session_prints_session_string() -> None:
    """Успешный вход: строка сессии печатается в stdout, код 0."""
    with _patch_telethon(_FakeClient):
        code, out = _run(["tg-session", "--api-id", "42", "--api-hash", "hashfoo"])
    assert code == 0
    assert "1BVtsOKFAKESESSIONSTRING999" in out
    assert "СЕКРЕТ" in out


def test_tg_session_returns_1_on_error() -> None:
    """Ошибка входа → код 1, сообщение в stdout."""

    class _BadClient(_FakeClient):
        async def start(self, **kwargs) -> None:
            raise RuntimeError("ApiIdInvalidError: bad credentials")

    with _patch_telethon(_BadClient):
        code, out = _run(["tg-session", "--api-id", "0", "--api-hash", "bad"])
    assert code == 1
    assert "Не удалось войти" in out


def test_tg_session_requires_arguments() -> None:
    """Аргументы --api-id и --api-hash обязательны."""
    with pytest.raises(SystemExit) as exc_info:
        _run(["tg-session"])
    assert exc_info.value.code != 0


def test_tg_session_does_not_write_files(tmp_path) -> None:
    """Команда не создаёт файлов сессии — только печатает строку."""
    before = set(os.listdir(tmp_path))
    with _patch_telethon(_FakeClient):
        _run(["tg-session", "--api-id", "42", "--api-hash", "hashfoo"])
    assert set(os.listdir(tmp_path)) == before
