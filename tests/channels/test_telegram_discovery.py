"""Tests for list_postable_dialogs — dialog discovery for forum-topic UI."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost.channels.telegram_discovery import list_postable_dialogs


def _channel(
    *,
    id: int,
    title: str,
    broadcast: bool = False,
    megagroup: bool = False,
    forum: bool = False,
    creator: bool = False,
    left: bool = False,
    username: str | None = None,
    access_hash: int = 111,
    admin_rights=None,
    banned_rights=None,
    default_banned_rights=None,
):
    from telethon.tl import types as tl

    return tl.Channel(
        id=id,
        title=title,
        photo=None,
        date=None,
        broadcast=broadcast,
        megagroup=megagroup,
        forum=forum,
        creator=creator,
        left=left,
        username=username,
        access_hash=access_hash,
        admin_rights=admin_rights,
        banned_rights=banned_rights,
        default_banned_rights=default_banned_rights,
    )


def _chat(*, id: int, title: str, deactivated: bool = False, left: bool = False):
    from telethon.tl import types as tl

    return tl.Chat(
        id=id,
        title=title,
        photo=None,
        participants_count=5,
        date=None,
        version=1,
        deactivated=deactivated,
        left=left,
    )


def _user(*, id: int = 1):
    from telethon.tl import types as tl

    return tl.User(id=id, access_hash=0)


def _admin_rights(*, post_messages: bool = True):
    from telethon.tl import types as tl

    return tl.ChatAdminRights(
        change_info=False,
        post_messages=post_messages,
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


def _banned_rights(*, send_messages: bool = False):
    from telethon.tl import types as tl

    return tl.ChatBannedRights(
        until_date=None,
        send_messages=send_messages,
    )


def _dialog(entity):
    d = MagicMock()
    d.entity = entity
    return d


def _client(*entities):
    client = AsyncMock()
    client.get_dialogs.return_value = [_dialog(e) for e in entities]
    return client


# ── broadcast channel ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_channel_creator_can_post():
    ch = _channel(id=100, title="My Chan", broadcast=True, creator=True, access_hash=42)
    result = await list_postable_dialogs(_client(ch))
    assert len(result) == 1
    r = result[0]
    assert r["target_value"] == "-100100"
    assert r["kind"] == "channel"
    assert r["can_post"] is True
    assert r["access_hash"] == 42
    assert r["title"] == "My Chan"


@pytest.mark.asyncio
async def test_broadcast_channel_admin_with_post_messages_can_post():
    ch = _channel(
        id=101, title="Chan", broadcast=True, admin_rights=_admin_rights(post_messages=True)
    )
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["can_post"] is True


@pytest.mark.asyncio
async def test_broadcast_channel_no_rights_cannot_post():
    ch = _channel(id=102, title="Chan", broadcast=True)
    result = await list_postable_dialogs(_client(ch))
    r = result[0]
    assert r["can_post"] is False
    assert r["reason"] is not None


# ── megagroup / forum ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_megagroup_kind_is_group():
    ch = _channel(id=200, title="Mega", megagroup=True)
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["kind"] == "group"


@pytest.mark.asyncio
async def test_forum_kind_is_forum():
    ch = _channel(id=201, title="Forum", megagroup=True, forum=True)
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["kind"] == "forum"


@pytest.mark.asyncio
async def test_megagroup_can_post_by_default():
    ch = _channel(id=202, title="Mega", megagroup=True)
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["can_post"] is True


@pytest.mark.asyncio
async def test_megagroup_banned_rights_cannot_post():
    ch = _channel(
        id=203,
        title="Mega",
        megagroup=True,
        banned_rights=_banned_rights(send_messages=True),
    )
    result = await list_postable_dialogs(_client(ch))
    r = result[0]
    assert r["can_post"] is False
    assert r["reason"] is not None


@pytest.mark.asyncio
async def test_megagroup_default_banned_rights_cannot_post():
    ch = _channel(
        id=204,
        title="Mega",
        megagroup=True,
        default_banned_rights=_banned_rights(send_messages=True),
    )
    result = await list_postable_dialogs(_client(ch))
    r = result[0]
    assert r["can_post"] is False


@pytest.mark.asyncio
async def test_megagroup_creator_overrides_default_banned():
    """Creator is never banned regardless of default_banned_rights."""
    ch = _channel(
        id=205,
        title="Mega",
        megagroup=True,
        creator=True,
        default_banned_rights=_banned_rights(send_messages=True),
    )
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["can_post"] is True


@pytest.mark.asyncio
async def test_megagroup_admin_overrides_default_banned():
    """Admin overrides default_banned_rights."""
    ch = _channel(
        id=206,
        title="Mega",
        megagroup=True,
        admin_rights=_admin_rights(),
        default_banned_rights=_banned_rights(send_messages=True),
    )
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["can_post"] is True


# ── basic group (Chat) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_group_can_post():
    chat = _chat(id=300, title="Group")
    result = await list_postable_dialogs(_client(chat))
    r = result[0]
    assert r["kind"] == "group"
    assert r["can_post"] is True
    assert r["target_value"] == "-300"
    assert r["access_hash"] is None


@pytest.mark.asyncio
async def test_basic_group_deactivated_cannot_post():
    chat = _chat(id=301, title="Dead Group", deactivated=True)
    result = await list_postable_dialogs(_client(chat))
    r = result[0]
    assert r["can_post"] is False
    assert r["reason"] is not None


# ── skipped entities ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_users_skipped():
    result = await list_postable_dialogs(_client(_user()))
    assert result == []


@pytest.mark.asyncio
async def test_left_channel_skipped():
    ch = _channel(id=400, title="Left", broadcast=True, left=True)
    result = await list_postable_dialogs(_client(ch))
    assert result == []


@pytest.mark.asyncio
async def test_left_basic_group_skipped():
    chat = _chat(id=401, title="Left Group", left=True)
    result = await list_postable_dialogs(_client(chat))
    assert result == []


# ── username passthrough ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_username_included():
    ch = _channel(id=500, title="Public", broadcast=True, creator=True, username="mypubchan")
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["username"] == "mypubchan"


@pytest.mark.asyncio
async def test_no_username_is_none():
    ch = _channel(id=501, title="Private", broadcast=True, creator=True)
    result = await list_postable_dialogs(_client(ch))
    assert result[0]["username"] is None


# ── mixed ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_dialogs_returns_all_non_user():
    entities = [
        _channel(id=600, title="Chan", broadcast=True, creator=True),
        _channel(id=601, title="Forum", megagroup=True, forum=True),
        _chat(id=602, title="Group"),
        _user(id=1),  # skipped
        _channel(id=603, title="Left", broadcast=True, left=True),  # skipped
    ]
    result = await list_postable_dialogs(_client(*entities))
    assert len(result) == 3
    kinds = {r["kind"] for r in result}
    assert kinds == {"channel", "forum", "group"}
