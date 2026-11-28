"""Local enrollment and atomic factor consumption; no plaintext persisted."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time

from cryptography.fernet import InvalidToken
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.auth.passwords import verify_password
from crosspost.auth.totp import generate_backup_codes, match_step
from crosspost.db.models import OwnerMfaEvent, Role, User
from crosspost.db.vault import decrypt_blob, encrypt_blob, get_vault


def _backup_digest(secret: str, user_id: int, code: str) -> str:
    payload = f"owner-mfa-backup-v1:{user_id}:{code}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


async def enroll_owner(
    session: AsyncSession,
    login: str,
    password: str,
    secret: str,
    code: str,
    *,
    now: int | None = None,
) -> list[str] | None:
    user = await session.scalar(select(User).where(User.login == login, User.role == Role.ADMIN))
    if user is None:
        await session.rollback()
        return None
    uid, generation, password_hash = user.id, user.session_generation, user.password_hash
    replaced = user.mfa_secret is not None
    await session.rollback()
    if not await asyncio.to_thread(verify_password, password, password_hash):
        return None
    timestamp = int(time.time()) if now is None else now
    step = match_step(secret, code, now=timestamp)
    if step is None:
        return None
    encrypted = encrypt_blob(get_vault(), secret)
    codes = generate_backup_codes()
    digests = [_backup_digest(secret, uid, value) for value in codes]
    result = await session.execute(
        update(User)
        .where(
            User.id == uid,
            User.role == Role.ADMIN,
            User.session_generation == generation,
            User.password_hash == password_hash,
        )
        .values(
            mfa_secret=encrypted,
            mfa_last_step=step,
            mfa_backup_hashes=json.dumps(digests),
            mfa_changed_at=timestamp,
            session_generation=User.session_generation + 1,
        )
        .returning(User.id)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        return None
    session.add(
        OwnerMfaEvent(
            user_id=uid,
            action="replaced" if replaced else "enrolled",
            generation=generation + 1,
            occurred_at=timestamp,
        )
    )
    await session.commit()
    return codes


async def consume_owner_factor(
    session: AsyncSession,
    user_id: int,
    generation: int,
    password_hash: str,
    code: str,
    *,
    now: int | None = None,
    commit: bool = True,
) -> bool:
    # Serialize both TOTP and backup consumption. No password hashing inside lock.
    await session.execute(text("BEGIN IMMEDIATE"))
    user = await session.get(User, user_id)
    if (
        user is None
        or user.role != Role.ADMIN
        or user.session_generation != generation
        or user.password_hash != password_hash
        or user.mfa_secret is None
        or user.mfa_last_step is None
        or user.mfa_backup_hashes is None
    ):
        await session.rollback()
        return False
    try:
        secret = decrypt_blob(get_vault(), user.mfa_secret)
        digests = json.loads(user.mfa_backup_hashes)
        if not isinstance(digests, list) or not all(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in digests
        ):
            raise ValueError("Invalid backup state")
        timestamp = int(time.time()) if now is None else now
        if re.fullmatch(r"[0-9]{6}", code):
            step = match_step(secret, code, now=timestamp, last_step=user.mfa_last_step)
            if step is None:
                await session.rollback()
                return False
            user.mfa_last_step = step
        elif re.fullmatch(r"[A-Za-z0-9_-]{22}", code):
            digest = _backup_digest(secret, user_id, code)
            remaining = [value for value in digests if not hmac.compare_digest(value, digest)]
            if len(remaining) != len(digests) - 1:
                await session.rollback()
                return False
            user.mfa_backup_hashes = json.dumps(remaining)
        else:
            await session.rollback()
            return False
    except (InvalidToken, ValueError, RuntimeError, TypeError):
        await session.rollback()
        return False
    if commit:
        await session.commit()
    return True
