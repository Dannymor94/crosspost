"""Telegram dialog/topic discovery + bulk target creation.

Admin-only endpoints: operators pick channels and topics to add as targets.
TTL cache: 10 min per profile / per (profile, target_value).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import update as sql_update

from crosspost.channels.telegram_discovery import list_forum_topics, list_postable_dialogs
from crosspost.channels.telegram_login import parse_credential_blob
from crosspost.db.models import ChannelTarget, CredentialKind
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.vault import get_vault
from crosspost.web.deps import RepoDep, SessionDep, require_admin

router = APIRouter()

_TTL = 600  # 10 min
_DIALOG_CACHE: dict[int, tuple[float, list]] = {}  # pid → (ts, items)
_TOPIC_CACHE: dict[tuple, tuple[float, list]] = {}  # (pid, target_value) → (ts, items)

_ADMIN_DEP = [Depends(require_admin)]


# ── Client builder ────────────────────────────────────────────────────────────


async def _build_discovery_client(cred: str):
    """Build a connected Telethon client from a credential blob. Caller must disconnect."""
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    cfg = parse_credential_blob(cred)
    api_id = int(cfg.get("api_id") or 0)
    api_hash = str(cfg.get("api_hash") or "")
    session = str(cfg.get("session") or "")
    if not (api_id and api_hash and session):
        return None
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    return client


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cache_fresh(ts: float) -> bool:
    return (time.time() - ts) < _TTL


# ── GET /dialogs ──────────────────────────────────────────────────────────────


@router.get(
    "/api/profiles/{profile_id}/channels/telegram/dialogs",
    dependencies=_ADMIN_DEP,
)
async def list_dialogs(
    profile_id: int,
    repo: RepoDep,
    refresh: bool = Query(False),
) -> dict:
    from telethon.errors import FloodWaitError  # noqa: PLC0415

    cached_items = _DIALOG_CACHE.get(profile_id, (0.0, []))[1]

    if not refresh and profile_id in _DIALOG_CACHE:
        ts, items = _DIALOG_CACHE[profile_id]
        if _cache_fresh(ts):
            return {"fetched_at": ts, "items": items}

    cred = await repo.get_credential(profile_id, "telegram", CredentialKind.API_TOKEN)
    if not cred:
        raise HTTPException(status_code=404, detail="Telegram не подключён")

    client = await _build_discovery_client(cred)
    if client is None:
        raise HTTPException(status_code=404, detail="Неполная учётка Telegram")

    try:
        items = await list_postable_dialogs(client)
    except FloodWaitError as e:
        return _flood_response(e.seconds, cached_items)
    finally:
        await client.disconnect()

    ts = time.time()
    _DIALOG_CACHE[profile_id] = (ts, items)
    return {"fetched_at": ts, "items": items}


# ── GET /dialogs/{target_value}/topics ────────────────────────────────────────


@router.get(
    "/api/profiles/{profile_id}/channels/telegram/dialogs/{target_value:path}/topics",
    dependencies=_ADMIN_DEP,
)
async def list_topics(
    profile_id: int,
    target_value: str,
    repo: RepoDep,
    refresh: bool = Query(False),
) -> dict:
    from telethon.errors import FloodWaitError  # noqa: PLC0415

    cache_key = (profile_id, target_value)
    cached_items = _TOPIC_CACHE.get(cache_key, (0.0, []))[1]

    if not refresh and cache_key in _TOPIC_CACHE:
        ts, items = _TOPIC_CACHE[cache_key]
        if _cache_fresh(ts):
            return {"fetched_at": ts, "items": items}

    cred = await repo.get_credential(profile_id, "telegram", CredentialKind.API_TOKEN)
    if not cred:
        raise HTTPException(status_code=404, detail="Telegram не подключён")

    client = await _build_discovery_client(cred)
    if client is None:
        raise HTTPException(status_code=404, detail="Неполная учётка Telegram")

    # resolve peer: use access_hash from dialog cache if available
    peer = _resolve_peer(profile_id, target_value)

    try:
        items = await list_forum_topics(client, peer)
    except FloodWaitError as e:
        return _flood_response(e.seconds, cached_items)
    finally:
        await client.disconnect()

    ts = time.time()
    _TOPIC_CACHE[cache_key] = (ts, items)
    return {"fetched_at": ts, "items": items}


def _resolve_peer(profile_id: int, target_value: str):
    """Return InputPeerChannel if we have access_hash from dialog cache, else raw string."""
    dialogs = _DIALOG_CACHE.get(profile_id, (0.0, []))[1]
    for d in dialogs:
        if (
            d["target_value"] == target_value
            and d.get("access_hash")
            and target_value.startswith("-100")
        ):
            try:
                from telethon.tl import types as tl  # noqa: PLC0415

                channel_id = int(target_value[4:])
                return tl.InputPeerChannel(channel_id=channel_id, access_hash=d["access_hash"])
            except Exception:  # noqa: BLE001
                pass
    return target_value


def _flood_response(seconds: int, cached_items: list) -> dict:

    # Return as dict; FastAPI will encode; status set via Response trick below.
    # We raise HTTPException so FastAPI sets the status code properly.
    raise _FloodException(seconds, cached_items)


class _FloodException(HTTPException):
    def __init__(self, seconds: int, items: list) -> None:
        super().__init__(
            status_code=429,
            detail={"retry_after": seconds, "items": items},
        )


# ── POST /targets/bulk ────────────────────────────────────────────────────────


class BulkTargetItem(BaseModel):
    target_value: str
    access_hash: int | None = None
    topic_id: int | None = None
    name: str | None = None


@router.post(
    "/api/profiles/{profile_id}/channels/telegram/targets/bulk",
    dependencies=_ADMIN_DEP,
)
async def bulk_create_targets(
    profile_id: int,
    body: list[BulkTargetItem],
    session: SessionDep,
) -> list[dict]:
    vault = get_vault()
    trepo = ChannelTargetRepository(session, vault=vault, profile_id=profile_id)
    existing = await trepo.list_targets("telegram")

    result = []
    for item in body:
        topic_id = None if (item.topic_id is None or item.topic_id == 1) else item.topic_id
        name = item.name or _default_name(profile_id, item.target_value, item.topic_id)

        # find existing match on (value, topic_id)
        match = _find_existing(trepo, existing, item.target_value, topic_id)

        topic_title = _get_topic_title(profile_id, item.target_value, topic_id)

        if match is not None:
            # backfill access_hash / topic_title if missing
            updates: dict = {}
            if item.access_hash is not None and match.peer_access_hash is None:
                updates["peer_access_hash"] = item.access_hash
            if topic_title and not getattr(match, "topic_title", None):
                updates["topic_title"] = topic_title
            if updates:
                await session.execute(
                    sql_update(ChannelTarget).where(ChannelTarget.id == match.id).values(**updates)
                )
                await session.commit()
            result.append(
                {
                    "target_id": match.id,
                    "name": trepo.decrypt_value(match) and _target_name(trepo, match),
                    "created": False,
                }
            )
        else:
            target = await trepo.add_target("telegram", name, item.target_value)
            extra: dict = {}
            if topic_id is not None:
                extra["topic_id"] = topic_id
            if item.access_hash is not None:
                extra["peer_access_hash"] = item.access_hash
            if topic_title:
                extra["topic_title"] = topic_title
            if extra:
                await session.execute(
                    sql_update(ChannelTarget).where(ChannelTarget.id == target.id).values(**extra)
                )
                await session.commit()
            result.append({"target_id": target.id, "name": name, "created": True})

    return result


def _find_existing(
    trepo: ChannelTargetRepository,
    targets: list[ChannelTarget],
    target_value: str,
    topic_id: int | None,
) -> ChannelTarget | None:
    for t in targets:
        try:
            val = trepo.decrypt_value(t)
        except Exception:  # noqa: BLE001
            continue
        if val == target_value and t.topic_id == topic_id:
            return t
    return None


def _target_name(trepo: ChannelTargetRepository, t: ChannelTarget) -> str:
    return t.name


def _get_topic_title(profile_id: int, target_value: str, topic_id: int | None) -> str | None:
    """Return topic title from in-memory cache, or None."""
    if topic_id is None:
        return None
    topics = _TOPIC_CACHE.get((profile_id, target_value), (0.0, []))[1]
    for t in topics:
        if t["topic_id"] == topic_id:
            return t["title"]
    return None


# ── POST /targets/from-link ───────────────────────────────────────────────────


class _FromLinkBody(BaseModel):
    url: str


@router.post(
    "/api/profiles/{profile_id}/channels/telegram/targets/from-link",
    dependencies=_ADMIN_DEP,
)
async def create_target_from_link(
    profile_id: int,
    body: _FromLinkBody,
    repo: RepoDep,
    session: SessionDep,
) -> dict:
    from crosspost.content.telegram_text import parse_telegram_link  # noqa: PLC0415

    try:
        target_value, raw_topic_id = parse_telegram_link(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    cred = await repo.get_credential(profile_id, "telegram", CredentialKind.API_TOKEN)
    if not cred:
        raise HTTPException(status_code=404, detail="Telegram не подключён")

    client = await _build_discovery_client(cred)
    if client is None:
        raise HTTPException(status_code=404, detail="Неполная учётка Telegram")

    try:
        entity = await client.get_entity(target_value)
    except Exception as exc:
        await client.disconnect()
        raise HTTPException(status_code=422, detail="Нет доступа к чату") from exc

    chat_title: str = getattr(entity, "title", None) or target_value
    access_hash: int | None = getattr(entity, "access_hash", None)

    topic_id = raw_topic_id
    topic_title: str | None = None
    if raw_topic_id is not None:
        try:
            topics = await list_forum_topics(client, entity)
            match = next((t for t in topics if t["topic_id"] == raw_topic_id), None)
            if match is not None:
                if not match["can_post"]:
                    await client.disconnect()
                    raise HTTPException(status_code=422, detail="Ветка закрыта")
                topic_title = match["title"]
            else:
                topic_id = None  # raw_topic_id was a message_id, not a forum topic
        except HTTPException:
            raise
        except Exception:
            topic_id = None  # channel is not a forum

    await client.disconnect()

    name = f"{chat_title} → {topic_title}" if topic_title else chat_title

    vault = get_vault()
    trepo = ChannelTargetRepository(session, vault=vault, profile_id=profile_id)
    existing = await trepo.list_targets("telegram")
    existing_match = _find_existing(trepo, existing, target_value, topic_id)

    if existing_match is not None:
        updates: dict = {}
        if access_hash is not None and existing_match.peer_access_hash is None:
            updates["peer_access_hash"] = access_hash
        if topic_title and not getattr(existing_match, "topic_title", None):
            updates["topic_title"] = topic_title
        if updates:
            await session.execute(
                sql_update(ChannelTarget)
                .where(ChannelTarget.id == existing_match.id)
                .values(**updates)
            )
            await session.commit()
        return {"target_id": existing_match.id, "name": existing_match.name, "created": False}

    target = await trepo.add_target("telegram", name, target_value)
    extra: dict = {}
    if topic_id is not None:
        extra["topic_id"] = topic_id
    if access_hash is not None:
        extra["peer_access_hash"] = access_hash
    if topic_title:
        extra["topic_title"] = topic_title
    if extra:
        await session.execute(
            sql_update(ChannelTarget).where(ChannelTarget.id == target.id).values(**extra)
        )
        await session.commit()
    return {"target_id": target.id, "name": name, "created": True}


def _default_name(profile_id: int, target_value: str, raw_topic_id: int | None) -> str:
    """Build '<chat title> → <topic title>' from cache, fall back to target_value."""
    topic_id = None if (raw_topic_id is None or raw_topic_id == 1) else raw_topic_id

    chat_title: str | None = None
    dialogs = _DIALOG_CACHE.get(profile_id, (0.0, []))[1]
    for d in dialogs:
        if d["target_value"] == target_value:
            chat_title = d["title"]
            break

    if chat_title is None:
        return target_value

    if topic_id is None:
        return chat_title

    topic_title: str | None = None
    topics = _TOPIC_CACHE.get((profile_id, target_value), (0.0, []))[1]
    for t in topics:
        if t["topic_id"] == topic_id:
            topic_title = t["title"]
            break

    if topic_title:
        return f"{chat_title} → {topic_title}"
    return chat_title
