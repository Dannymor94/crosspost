from __future__ import annotations

import pytest

from crosspost.orchestrator.task import JSONIdempotencyStore, new_publication_id


@pytest.mark.asyncio
async def test_json_store_persists_and_dedups(tmp_path):
    path = tmp_path / "state.json"
    pid = new_publication_id()

    s1 = JSONIdempotencyStore(path)
    assert await s1.is_done(pid, "telegram") is False
    await s1.mark_done(pid, "telegram", external_id="100")
    assert await s1.is_done(pid, "telegram") is True

    # переоткрытие из файла — состояние сохранилось
    s2 = JSONIdempotencyStore(path)
    assert await s2.is_done(pid, "telegram") is True
    assert await s2.is_done(pid, "vk") is False  # другой канал — не done
