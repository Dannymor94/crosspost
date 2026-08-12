"""Тесты единого секвенсора связки статья→каналы (orchestrator/article_flow).

Чистая оркестрация: политика ретраев и запись в БД инъектируются через step/fail_closed.
Тестируем на fake-step без БД: порядок, подстановку url, fail-closed, pending, идемпотентность.
"""

from __future__ import annotations

import pytest

from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.orchestrator.article_flow import (
    _ARTICLE_CHANNEL,
    FlowResult,
    StepResult,
    StepState,
    run_binding,
)
from crosspost.orchestrator.publish_service import ChannelOutcome

pytestmark = pytest.mark.asyncio


def _post(text="Короткий"):
    return CanonicalContent(type=ContentType.POST, text=text)


class _Recorder:
    """Фейковый step: пишет вызовы (channel, text), отдаёт заранее заданный StepState."""

    def __init__(self, plan: dict[str, StepState], article_url="https://telegra.ph/X-08-12"):
        self.plan = plan
        self.article_url = article_url
        self.calls: list[tuple[str, str, ContentType]] = []

    async def step(self, channel: str, content: CanonicalContent) -> StepResult:
        self.calls.append((channel, content.text, content.type))
        state = self.plan.get(channel, StepState.DONE)
        ext = self.article_url if channel == _ARTICLE_CHANNEL else f"id-{channel}"
        ext = ext if state is StepState.DONE else None
        return StepResult(state, ChannelOutcome(channel, state.name.lower(), external_id=ext), ext)

    async def fail_closed(self, channel: str) -> ChannelOutcome:
        self.calls.append((channel, "<FAIL_CLOSED>", ContentType.POST))
        return ChannelOutcome(channel, "failed", error="no-article")


async def _run(
    rec, *, article_text="", channels=("telegram",), content=None, link_phrase="Подробнее"
):
    return await run_binding(
        content=content or _post(),
        channels=list(channels),
        article_text=article_text,
        article_title="Заголовок",
        template="{text} → {url} ({phrase})",
        link_phrase=link_phrase,
        step=rec.step,
        fail_closed=rec.fail_closed,
    )


async def test_no_article_runs_channels_directly():
    rec = _Recorder({})
    res = await _run(rec, article_text="", channels=("telegram", "vk_wall"))
    assert isinstance(res, FlowResult)
    assert [c[0] for c in rec.calls] == ["telegram", "vk_wall"]
    assert res.pending is False
    assert _ARTICLE_CHANNEL not in [c[0] for c in rec.calls]


async def test_no_article_pending_propagates():
    rec = _Recorder({"telegram": StepState.PENDING})
    res = await _run(rec, article_text="", channels=("telegram",))
    assert res.pending is True


async def test_happy_path_article_first_then_url_substituted():
    rec = _Recorder({}, article_url="https://telegra.ph/Long-08-12")
    res = await _run(rec, article_text="тело лонгрида", channels=("telegram",))
    # Порядок: сначала telegraph, потом канал.
    assert rec.calls[0][0] == _ARTICLE_CHANNEL
    assert rec.calls[0][1] == "тело лонгрида"  # телом статьи ушёл article_text
    assert rec.calls[1][0] == "telegram"
    # В текст канала подставлен url статьи по шаблону "{text} → {url}".
    assert rec.calls[1][1] == "Короткий → https://telegra.ph/Long-08-12 (Подробнее)"
    assert res.pending is False
    # Итог статьи присутствует в outcomes.
    assert any(o.channel == _ARTICLE_CHANNEL for o in res.outcomes)


async def test_article_failed_fail_closed_channels_not_published():
    rec = _Recorder({_ARTICLE_CHANNEL: StepState.FAILED})
    res = await _run(rec, article_text="тело", channels=("telegram", "vk_wall"))
    # telegraph вызван, каналы — только через fail_closed (не публикация).
    assert rec.calls[0][0] == _ARTICLE_CHANNEL
    fc = [c for c in rec.calls if c[1] == "<FAIL_CLOSED>"]
    assert {c[0] for c in fc} == {"telegram", "vk_wall"}
    # Реальной публикации в telegram/vk_wall не было.
    published = [
        c for c in rec.calls if c[0] in ("telegram", "vk_wall") and c[1] != "<FAIL_CLOSED>"
    ]
    assert not published
    assert res.pending is False
    # Итоги содержат fail-closed каналы + статью.
    assert {o.channel for o in res.outcomes} == {"telegram", "vk_wall", _ARTICLE_CHANNEL}


async def test_article_pending_does_not_touch_channels():
    rec = _Recorder({_ARTICLE_CHANNEL: StepState.PENDING})
    res = await _run(rec, article_text="тело", channels=("telegram",))
    assert [c[0] for c in rec.calls] == [_ARTICLE_CHANNEL]  # каналов не касались
    assert res.pending is True
    assert res.outcomes == []


async def test_channel_pending_after_article_propagates():
    rec = _Recorder({"telegram": StepState.PENDING})
    res = await _run(rec, article_text="тело", channels=("telegram",))
    assert rec.calls[0][0] == _ARTICLE_CHANNEL
    assert res.pending is True  # статья готова, но канал ещё висит
