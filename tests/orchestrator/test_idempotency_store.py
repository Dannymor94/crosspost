from __future__ import annotations

from crosspost.orchestrator.task import JSONIdempotencyStore, new_publication_id


def test_json_store_persists_and_dedups(tmp_path):
    path = tmp_path / "state.json"
    pid = new_publication_id()

    s1 = JSONIdempotencyStore(path)
    assert s1.is_done(pid, "telegram") is False
    s1.mark_done(pid, "telegram", external_id="100")
    assert s1.is_done(pid, "telegram") is True

    # переоткрытие из файла — состояние сохранилось
    s2 = JSONIdempotencyStore(path)
    assert s2.is_done(pid, "telegram") is True
    assert s2.is_done(pid, "vk") is False  # другой канал — не done
