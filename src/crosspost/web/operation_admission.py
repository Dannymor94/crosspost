"""Opt-in immediate HTTP admission; no automatic re-dispatch or result disclosure."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.operation_repo import OperationConflict, OperationRepository

_CHUNK_SIZE = 1024 * 1024


async def immediate_fingerprint(fields: dict, media: Sequence[UploadFile]) -> str:
    """Versioned exact fields, canonical targets, ordered media bytes and metadata.

    External image is a source identifier, not a claim that its contents are immutable.
    Hash the uploads without changing the subsequent upload reader's initial position.
    """
    uploads = []
    for upload in media:
        if not upload.filename:
            continue
        digest = hashlib.sha256()
        await upload.seek(0)
        try:
            while chunk := await upload.read(_CHUNK_SIZE):
                digest.update(chunk)
        finally:
            await upload.seek(0)
        uploads.append([upload.filename, upload.content_type, digest.hexdigest()])
    payload = {"version": 1, "kind": "immediate", "fields": fields, "uploads": uploads}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def reserve_immediate(
    session: AsyncSession,
    *,
    actor_id: int,
    profile_id: int,
    operation_key: str,
    fields: dict,
    media: Sequence[UploadFile],
) -> str:
    """Only the first committed reservation may proceed to media save and send.

    Replays and mismatches intentionally receive the same generic 409 until
    identity lifecycle and scoped result lookup are implemented. No old receipt
    or publication identifier is disclosed, including after account ID reuse.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", operation_key):
        raise HTTPException(status_code=422, detail="Invalid Idempotency-Key")
    fingerprint = await immediate_fingerprint(fields, media)
    # The route has only performed reads at this point. End that read transaction
    # before acquiring SQLite's write reservation.
    await session.rollback()
    repo = OperationRepository(session, actor_id=actor_id, profile_id=profile_id)
    try:
        reservation = await repo.reserve(operation_key, kind="immediate", fingerprint=fingerprint)
    except OperationConflict:
        reservation = None
    if reservation is None or not reservation.acquired:
        raise HTTPException(
            status_code=409,
            detail="Операция уже использована. Не отправляйте повторно; проверьте результат.",
        )
    return reservation.publication_id
