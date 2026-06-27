"""Первые тесты TG-адаптера (фаза A, TDD).

ОДИН тест зелёный сразу (идемпотентный пропуск — логика уже есть),
ОДИН красный (реальная публикация ещё не реализована — это следующая работа).
"""
from __future__ import annotations

import pytest

from crosspost.adapters.api.telegram import TelegramAdapter
from crosspost.adapters.base import ResultStatus


@pytest.mark.asyncio
async def test_skips_when_already_published(store, publication_id, sample_post, fake_telethon_client):
    """Если (publication_id, channel) уже done — не публикуем повторно (без дублей)."""
    store.mark_done(publication_id, "telegram", external_id="999")
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    fake_telethon_client.send_file.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_and_returns_receipt(store, publication_id, sample_post, fake_telethon_client):
    """RED: первая публикация отправляет медиа и возвращает квитанцию (external_id).

    Падает, пока publish не реализован. Это и есть следующая задача фазы A.
    """
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id is not None              # квитанция получена
    fake_telethon_client.send_file.assert_called_once()  # медиа ушло один раз
    assert store.is_done(publication_id, "telegram")     # помечено done
