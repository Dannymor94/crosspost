"""Enumerate postable dialogs from a Telethon client session."""

from __future__ import annotations

from typing import TypedDict


class DialogInfo(TypedDict):
    target_value: str  # "-100<id>" for Channel, "-<id>" for basic Chat
    access_hash: int | None
    title: str
    kind: str  # "channel" | "group" | "forum"
    username: str | None
    can_post: bool
    reason: str | None  # human-readable reason when can_post=False


async def list_postable_dialogs(client) -> list[DialogInfo]:
    """Iterate account dialogs and classify each as postable or not.

    Skips: users, bots, dialogs the account has left.
    """
    from telethon.tl import types as tl

    dialogs = await client.get_dialogs()
    result: list[DialogInfo] = []

    for dialog in dialogs:
        entity = dialog.entity

        if isinstance(entity, tl.User):
            continue

        if isinstance(entity, (tl.Chat, tl.ChatForbidden)):
            if isinstance(entity, tl.ChatForbidden):
                continue
            if getattr(entity, "left", False):
                continue
            can_post = not entity.deactivated
            result.append(
                DialogInfo(
                    target_value=f"-{entity.id}",
                    access_hash=None,
                    title=entity.title,
                    kind="group",
                    username=None,
                    can_post=can_post,
                    reason="deactivated" if not can_post else None,
                )
            )
            continue

        if isinstance(entity, tl.Channel):
            if getattr(entity, "left", False):
                continue

            if entity.broadcast:
                kind = "channel"
                can_post, reason = _channel_can_post(entity)
            else:
                # megagroup
                kind = "forum" if getattr(entity, "forum", False) else "group"
                can_post, reason = _megagroup_can_post(entity)

            result.append(
                DialogInfo(
                    target_value=f"-100{entity.id}",
                    access_hash=getattr(entity, "access_hash", None),
                    title=entity.title,
                    kind=kind,
                    username=getattr(entity, "username", None) or None,
                    can_post=can_post,
                    reason=reason,
                )
            )

    return result


def _channel_can_post(entity) -> tuple[bool, str | None]:
    """Broadcast channel: creator or admin with post_messages."""
    if getattr(entity, "creator", False):
        return True, None
    ar = getattr(entity, "admin_rights", None)
    if ar is not None and getattr(ar, "post_messages", False):
        return True, None
    return False, "no post_messages right"


class TopicInfo(TypedDict):
    topic_id: int | None  # None for General (id=1)
    title: str
    icon_emoji: int | None
    closed: bool
    can_post: bool
    reason: str | None


async def list_forum_topics(client, peer, *, limit: int = 100) -> list[TopicInfo]:
    """Fetch all forum topics for a Channel entity, paginating as needed.

    Uses functions.messages.GetForumTopicsRequest (Telethon 1.44).
    Skips ForumTopicDeleted entries.
    """
    from telethon.tl import functions
    from telethon.tl import types as tl

    is_privileged = getattr(peer, "creator", False) or (
        getattr(peer, "admin_rights", None) is not None
    )

    result: list[TopicInfo] = []
    offset_date = 0
    offset_id = 0
    offset_topic = 0

    while True:
        resp = await client(
            functions.messages.GetForumTopicsRequest(
                peer=peer,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=limit,
            )
        )

        for topic in resp.topics:
            if isinstance(topic, tl.ForumTopicDeleted):
                continue

            closed = bool(getattr(topic, "closed", False))
            if closed and not is_privileged:
                can_post, reason = False, "ветка закрыта"
            else:
                can_post, reason = True, None

            result.append(
                TopicInfo(
                    topic_id=None if topic.id == 1 else topic.id,
                    title=topic.title,
                    icon_emoji=getattr(topic, "icon_emoji_id", None) or None,
                    closed=closed,
                    can_post=can_post,
                    reason=reason,
                )
            )

        if len(resp.topics) < limit:
            break

        last = resp.topics[-1]
        offset_topic = last.id
        offset_date = getattr(last, "date", 0) or 0
        offset_id = getattr(last, "top_message", 0) or 0

    return result


def _megagroup_can_post(entity) -> tuple[bool, str | None]:
    """Megagroup: creator/admin bypass bans; banned_rights > default_banned_rights."""
    if getattr(entity, "creator", False):
        return True, None
    if getattr(entity, "admin_rights", None) is not None:
        return True, None
    # personal ban takes priority over default
    br = getattr(entity, "banned_rights", None)
    if br is not None and getattr(br, "send_messages", False):
        return False, "send_messages banned"
    dbr = getattr(entity, "default_banned_rights", None)
    if dbr is not None and getattr(dbr, "send_messages", False):
        return False, "send_messages banned by default"
    return True, None
