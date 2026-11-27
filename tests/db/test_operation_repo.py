"""T-216: persistent, one-winner admission without publication side effects."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.operation_repo import OperationConflict, OperationRepository

KEY = "a" * 32
DIGEST = "b" * 64


async def test_reservation_is_durable_and_single_winner(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}"
    engine = await create_engine_and_tables(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def reserve():
            async with factory() as session:
                repo = OperationRepository(session, profile_id=1, actor_id=1)
                return await repo.reserve(KEY, kind="immediate", fingerprint=DIGEST)

        results = await asyncio.gather(*(reserve() for _ in range(4)))
        assert sum(r.acquired for r in results) == 1
        assert len({r.publication_id for r in results}) == 1
        original_id = results[0].publication_id
    finally:
        await engine.dispose()

    reopened = await create_engine_and_tables(url)
    try:
        factory = async_sessionmaker(reopened, expire_on_commit=False)
        async with factory() as session:
            repo = OperationRepository(session, profile_id=1, actor_id=1)
            replay = await repo.reserve(KEY, kind="immediate", fingerprint=DIGEST)
            assert not replay.acquired
            assert replay.publication_id == original_id
            new = await repo.reserve("c" * 32, kind="immediate", fingerprint=DIGEST)
            assert new.acquired
            assert new.publication_id != original_id
    finally:
        await reopened.dispose()


@pytest.mark.parametrize(
    ("profile_id", "actor_id", "kind", "digest"),
    [
        (2, 1, "immediate", DIGEST),
        (1, 2, "immediate", DIGEST),
        (1, 1, "scheduled", DIGEST),
        (1, 1, "immediate", "d" * 64),
    ],
)
async def test_conflict_does_not_transfer_reservation(profile_id, actor_id, kind, digest):
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            repo = OperationRepository(session, profile_id=1, actor_id=1)
            original = await repo.reserve(KEY, kind="immediate", fingerprint=DIGEST)
        async with factory() as session:
            other = OperationRepository(session, profile_id=profile_id, actor_id=actor_id)
            with pytest.raises(OperationConflict):
                await other.reserve(KEY, kind=kind, fingerprint=digest)
        async with factory() as session:
            repo = OperationRepository(session, profile_id=1, actor_id=1)
            replay = await repo.reserve(KEY, kind="immediate", fingerprint=DIGEST)
            assert not replay.acquired
            assert replay.publication_id == original.publication_id
    finally:
        await engine.dispose()
