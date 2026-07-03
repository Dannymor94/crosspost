from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.orchestrator.task import InMemoryIdempotencyStore, new_publication_id


@pytest.fixture
def store() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def publication_id() -> str:
    return new_publication_id()


@pytest.fixture
def sample_post(tmp_path: Path) -> CanonicalContent:
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff")  # минимальный jpeg-заголовок
    return CanonicalContent(type=ContentType.POST, text="Привет", media_paths=[img])


@pytest.fixture
def fake_telethon_client() -> AsyncMock:
    """Мок Telethon: send_file с одним медиа → одно сообщение (объект с .id)."""
    client = AsyncMock()
    sent = AsyncMock()
    sent.id = 12345
    client.send_file.return_value = sent
    return client


@pytest.fixture
def fake_telethon_client_album(tmp_path: Path) -> AsyncMock:
    """Мок Telethon: send_file с несколькими медиа → список сообщений (альбом)."""
    client = AsyncMock()
    msg1, msg2 = AsyncMock(), AsyncMock()
    msg1.id = 100
    msg2.id = 101
    client.send_file.return_value = [msg1, msg2]
    return client


@pytest.fixture
def sample_album(tmp_path: Path) -> CanonicalContent:
    """Пост с двумя медиа — Telethon отправит как альбом."""
    img1 = tmp_path / "img1.jpg"
    img2 = tmp_path / "img2.jpg"
    img1.write_bytes(b"\xff\xd8\xff")
    img2.write_bytes(b"\xff\xd8\xff")
    return CanonicalContent(type=ContentType.POST, text="Альбом", media_paths=[img1, img2])
