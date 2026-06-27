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
    """Мок Telethon: send_file возвращает объект с .id (квитанция)."""
    client = AsyncMock()
    sent = AsyncMock()
    sent.id = 12345
    client.send_file.return_value = sent
    return client
