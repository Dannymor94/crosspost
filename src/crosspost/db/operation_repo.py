"""T-216 admission primitive. No external effects and no automatic re-acquisition."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import PublicationOperation
from crosspost.orchestrator.task import new_publication_id


REJECTED_BEFORE_EFFECT = "rejected_before_effect"


def rejected_before_effect(summary: list | None) -> bool:
    return (
        isinstance(summary, list)
        and len(summary) == 1
        and isinstance(summary[0], dict)
        and summary[0].get("status") == REJECTED_BEFORE_EFFECT
    )


class OperationConflict(Exception):
    """Key is bound to another request or scope; never expose the existing row."""


@dataclass(frozen=True)
class Reservation:
    publication_id: str
    acquired: bool


class OperationRepository:
    """Reserve once in SQLite, using a dedicated short-lived session.

    An acquired reservation MUST commit before an external side effect.
    A replay only returns its stable ID: it must not dispatch, even after restart.
    Authenticated actor and profile are supplied by the server, not the body.
    """

    def __init__(self, session: AsyncSession, *, profile_id: int, actor_id: int) -> None:
        self._s = session
        self._profile_id = profile_id
        self._actor_id = actor_id

    async def reserve(
        self,
        operation_id: str,
        *,
        kind: str,
        fingerprint: str,
        recovery_hash: str | None = None,
        commit: bool = True,
    ) -> Reservation:
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("invalid operation ID")
        if kind not in {"immediate", "scheduled"}:
            raise ValueError("invalid operation kind")
        if not commit and kind != "scheduled":
            raise ValueError("only scheduled admission may share a transaction")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("invalid request fingerprint")
        if self._profile_id <= 0 or self._actor_id <= 0:
            raise ValueError("invalid operation scope")

        if recovery_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", recovery_hash):
            raise ValueError("invalid recovery proof")

        candidate = new_publication_id()
        stmt = (
            insert(PublicationOperation)
            .values(
                operation_id=operation_id,
                profile_id=self._profile_id,
                actor_id=self._actor_id,
                kind=kind,
                fingerprint=fingerprint,
                publication_id=candidate,
                recovery_hash=recovery_hash,
            )
            .on_conflict_do_nothing(index_elements=["operation_id"])
            .returning(PublicationOperation.publication_id)
        )
        try:
            inserted = (await self._s.execute(stmt)).scalar_one_or_none()
            if inserted is not None:
                if commit:
                    await self._s.commit()
                return Reservation(publication_id=inserted, acquired=True)
            result = await self._s.execute(
                select(PublicationOperation).where(
                    PublicationOperation.operation_id == operation_id,
                    PublicationOperation.profile_id == self._profile_id,
                    PublicationOperation.actor_id == self._actor_id,
                    PublicationOperation.kind == kind,
                    PublicationOperation.fingerprint == fingerprint,
                    PublicationOperation.recovery_hash == recovery_hash,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                raise OperationConflict("operation key conflict")
            publication_id = existing.publication_id
            if commit:
                await self._s.commit()
            return Reservation(publication_id=publication_id, acquired=False)
        except BaseException:
            await self._s.rollback()
            raise

    async def finish(self, publication_id: str, summary: list[dict[str, str]]) -> None:
        """Write the first observed result, never overwrite an existing summary."""
        result = await self._s.execute(
            sql_update(PublicationOperation)
            .where(
                PublicationOperation.publication_id == publication_id,
                PublicationOperation.profile_id == self._profile_id,
                PublicationOperation.actor_id == self._actor_id,
                PublicationOperation.result_summary.is_(None),
            )
            .values(result_summary=summary)
        )
        if getattr(result, "rowcount", 0) != 1:
            await self._s.rollback()
            raise OperationConflict("operation completion conflict")
        await self._s.commit()

    async def recover(self, operation_id: str, recovery_hash: str) -> PublicationOperation | None:
        """Read-only lookup requires a separate recovery proof and matching scope."""
        result = await self._s.execute(
            select(PublicationOperation).where(
                PublicationOperation.operation_id == operation_id,
                PublicationOperation.profile_id == self._profile_id,
                PublicationOperation.actor_id == self._actor_id,
                PublicationOperation.recovery_hash == recovery_hash,
            )
        )
        return result.scalar_one_or_none()

    async def exists(self, operation_id: str) -> bool:
        """Admission preflight only; caller returns the same generic conflict."""
        result = await self._s.execute(
            select(PublicationOperation.operation_id).where(
                PublicationOperation.operation_id == operation_id
            )
        )
        return result.scalar_one_or_none() is not None
