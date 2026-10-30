"""Tests for list_forum_topics — paginated forum topic discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost.channels.telegram_discovery import list_forum_topics


def _topic(
    *,
    id: int,
    title: str,
    closed: bool = False,
    icon_emoji_id: int | None = None,
):
    from telethon.tl import types as tl

    return tl.ForumTopic(
        id=id,
        date=None,
        peer=None,
        title=title,
        icon_color=0,
        top_message=id * 10,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=None,
        notify_settings=None,
        closed=closed,
        icon_emoji_id=icon_emoji_id,
    )


def _deleted(id: int):
    from telethon.tl import types as tl

    return tl.ForumTopicDeleted(id=id)


def _forum_topics_response(topics, *, count=None):
    resp = MagicMock()
    resp.topics = topics
    resp.count = count if count is not None else len(topics)
    return resp


def _channel(*, id: int = 999, creator: bool = False, admin_rights=None):
    """Minimal Channel entity for admin/creator checks."""
    from telethon.tl import types as tl

    return tl.Channel(
        id=id,
        title="Forum",
        photo=None,
        date=None,
        megagroup=True,
        forum=True,
        creator=creator,
        admin_rights=admin_rights,
    )


def _admin_rights():
    from telethon.tl import types as tl

    return tl.ChatAdminRights(
        change_info=False,
        post_messages=True,
        edit_messages=False,
        delete_messages=False,
        ban_users=False,
        invite_users=False,
        pin_messages=False,
        add_admins=False,
        anonymous=False,
        manage_call=False,
        other=False,
    )


def _client(pages: list, entity=None):
    """Callable async mock: client(request) returns successive page responses."""
    responses = [_forum_topics_response(p) for p in pages]
    client = AsyncMock(side_effect=responses)
    if entity is None:
        entity = _channel()
    return client, entity


# ── single page ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_page_returns_all_topics():
    topics = [_topic(id=1, title="General"), _topic(id=5, title="Intro")]
    client, entity = _client([topics])

    result = await list_forum_topics(client, entity)

    assert len(result) == 2
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_general_topic_id_1_normalised_to_none():
    topics = [_topic(id=1, title="General"), _topic(id=7, title="Other")]
    client, entity = _client([topics])

    result = await list_forum_topics(client, entity)

    general = next(r for r in result if r["title"] == "General")
    assert general["topic_id"] is None

    other = next(r for r in result if r["title"] == "Other")
    assert other["topic_id"] == 7


# ── pagination ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pagination_fetches_all_pages():
    """Two full pages + one partial → 3 requests total."""
    limit = 2
    page1 = [_topic(id=5, title="A"), _topic(id=3, title="B")]
    page2 = [_topic(id=2, title="C"), _topic(id=1, title="General")]
    page3 = []  # empty → stop

    client, entity = _client([page1, page2, page3])

    result = await list_forum_topics(client, entity, limit=limit)

    assert len(result) == 4
    assert client.call_count == 3


@pytest.mark.asyncio
async def test_pagination_stops_on_partial_page():
    """A page shorter than limit signals last page."""
    limit = 3
    page1 = [_topic(id=10, title="A"), _topic(id=9, title="B"), _topic(id=8, title="C")]
    page2 = [_topic(id=7, title="D")]  # 1 < limit → last page

    client, entity = _client([page1, page2])

    result = await list_forum_topics(client, entity, limit=limit)

    assert len(result) == 4
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_pagination_passes_correct_offsets():
    """Second request uses offset from last topic of first page."""
    from telethon.tl import functions

    limit = 2
    t_a = _topic(id=10, title="A")
    t_b = _topic(id=5, title="B")
    page1 = [t_a, t_b]
    page2 = []

    client, entity = _client([page1, page2])
    await list_forum_topics(client, entity, limit=limit)

    second_call = client.call_args_list[1]
    req = second_call.args[0]
    assert isinstance(req, functions.messages.GetForumTopicsRequest)
    assert req.offset_topic == t_b.id
    assert req.offset_date == (t_b.date or 0)
    assert req.offset_id == (t_b.top_message or 0)


# ── deleted topics skipped ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleted_topics_skipped():
    topics = [_topic(id=5, title="Alive"), _deleted(id=3), _topic(id=1, title="General")]
    client, entity = _client([topics])

    result = await list_forum_topics(client, entity)

    ids = [r["topic_id"] for r in result]
    assert 3 not in ids
    assert len(result) == 2


# ── closed topics ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_closed_topic_non_admin_cannot_post():
    topics = [_topic(id=5, title="Closed", closed=True)]
    entity = _channel(creator=False)
    client, _ = _client([topics], entity=entity)

    result = await list_forum_topics(client, entity)

    r = result[0]
    assert r["closed"] is True
    assert r["can_post"] is False
    assert r["reason"] == "ветка закрыта"


@pytest.mark.asyncio
async def test_closed_topic_creator_can_post():
    topics = [_topic(id=5, title="Closed", closed=True)]
    entity = _channel(creator=True)
    client, _ = _client([topics], entity=entity)

    result = await list_forum_topics(client, entity)

    r = result[0]
    assert r["can_post"] is True
    assert r["reason"] is None


@pytest.mark.asyncio
async def test_closed_topic_admin_can_post():
    topics = [_topic(id=5, title="Closed", closed=True)]
    entity = _channel(admin_rights=_admin_rights())
    client, _ = _client([topics], entity=entity)

    result = await list_forum_topics(client, entity)

    assert result[0]["can_post"] is True


@pytest.mark.asyncio
async def test_open_topic_anyone_can_post():
    topics = [_topic(id=5, title="Open", closed=False)]
    entity = _channel()
    client, _ = _client([topics], entity=entity)

    result = await list_forum_topics(client, entity)

    assert result[0]["can_post"] is True
    assert result[0]["reason"] is None


# ── icon_emoji ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_icon_emoji_passed_through():
    topics = [_topic(id=5, title="Emoji", icon_emoji_id=12345678)]
    client, entity = _client([topics])

    result = await list_forum_topics(client, entity)

    assert result[0]["icon_emoji"] == 12345678


@pytest.mark.asyncio
async def test_no_icon_emoji_is_none():
    topics = [_topic(id=5, title="NoEmoji")]
    client, entity = _client([topics])

    result = await list_forum_topics(client, entity)

    assert result[0]["icon_emoji"] is None
