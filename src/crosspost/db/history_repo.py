"""Profile-scoped history read model; observed outcomes are not complete receipts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import String, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import Publication, ScheduledPost

Source = Literal["scheduled", "immediate"]
SourceFilter = Literal["all", "scheduled", "immediate"]


class HistoryCursor(BaseModel):
    stamp: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")
    source: Source
    key: str = Field(min_length=1, max_length=64)
    filter_key: str = Field(pattern=r"^[0-9a-f]{64}$")

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, token: str) -> HistoryCursor:
        try:
            if len(token) > 512:
                raise ValueError("cursor too long")
            return cls.model_validate_json(base64.b64decode(token, altchars=b"-_", validate=True))
        except (ValueError, ValidationError, binascii.Error) as exc:
            raise ValueError("invalid cursor") from exc


class HistoryOutcome(BaseModel):
    channel: str
    target: str
    status: str


class HistoryEntry(BaseModel):
    source: Source
    key: str
    created_at_utc: str
    text: str | None
    status: str
    outcomes: list[HistoryOutcome]
    coverage: Literal["observed_only"] = "observed_only"


class HistoryPage(BaseModel):
    entries: list[HistoryEntry]
    next_cursor: str | None
    scope: Literal["scheduled_and_observed_immediate"] = "scheduled_and_observed_immediate"


_KNOWN = frozenset({"done", "submitted", "failed", "needs_relogin", "attempting", "queued", "new"})


def observed_status(outcomes: list[HistoryOutcome]) -> str:
    statuses = {o.status for o in outcomes}
    if not statuses or not statuses <= _KNOWN:
        return "unknown"
    if len(statuses) > 1:
        return "mixed_observed"
    status = next(iter(statuses))
    if status in {"new", "queued", "attempting"}:
        return "in_progress_observed"
    return f"{status}_observed"


class HistoryRepository:
    def __init__(self, session: AsyncSession, profile_id: int) -> None:
        self.session = session
        self.profile_id = profile_id

    async def page(
        self,
        *,
        limit: int,
        before: HistoryCursor | None = None,
        source_filter: SourceFilter = "all",
        query: str = "",
    ) -> HistoryPage:
        # Bind cursor to the authorized scope and exact search. It is not a
        # credential and contains no plaintext search query.
        filter_key = hashlib.sha256(
            json.dumps([self.profile_id, source_filter, query], ensure_ascii=False).encode()
        ).hexdigest()
        if before is not None and before.filter_key != filter_key:
            raise ValueError("cursor does not match search")
        # Normalize SQLite datetime precision so server-default and Python dates
        # compare consistently. Source + key break ties at the millisecond.
        stamp_format = "%Y-%m-%dT%H:%M:%f"
        scheduled = select(
            literal("scheduled").label("source"),
            cast(ScheduledPost.id, String).label("key"),
            func.strftime(stamp_format, ScheduledPost.created_at).label("stamp"),
        ).where(ScheduledPost.profile_id == self.profile_id)
        immediate = (
            select(
                literal("immediate").label("source"),
                Publication.publication_id.label("key"),
                func.strftime(stamp_format, func.min(Publication.created_at)).label("stamp"),
            )
            .where(
                Publication.profile_id == self.profile_id,
                ~Publication.publication_id.like("schedpost-%"),
            )
            .group_by(Publication.publication_id)
        )
        if query:
            # SQLite instr is a literal, case-sensitive substring search.
            # Do not search raw adapter errors, external IDs or filesystem paths.
            channel_match = func.instr(Publication.channel, query) > 0
            scheduled_match = (
                select(Publication.id)
                .where(
                    Publication.profile_id == self.profile_id,
                    Publication.publication_id
                    == literal("schedpost-") + cast(ScheduledPost.id, String),
                    channel_match,
                )
                .correlate(ScheduledPost)
                .exists()
            )
            scheduled = scheduled.where(
                or_(func.instr(ScheduledPost.text, query) > 0, scheduled_match)
            )
            # HAVING preserves ALL observed outcomes in the group and the
            # original first-created timestamp, even if only one channel matches.
            immediate = immediate.having(func.max(case((channel_match, 1), else_=0)) == 1)
        keys = union_all(scheduled, immediate).subquery()
        stmt = select(keys).where(keys.c.stamp.is_not(None))
        if source_filter != "all":
            stmt = stmt.where(keys.c.source == source_filter)
        if before is not None:
            stmt = stmt.where(
                or_(
                    keys.c.stamp < before.stamp,
                    and_(keys.c.stamp == before.stamp, keys.c.source < before.source),
                    and_(
                        keys.c.stamp == before.stamp,
                        keys.c.source == before.source,
                        keys.c.key < before.key,
                    ),
                )
            )
        stmt = stmt.order_by(keys.c.stamp.desc(), keys.c.source.desc(), keys.c.key.desc()).limit(
            limit + 1
        )
        rows = (await self.session.execute(stmt)).mappings().all()
        entries: list[HistoryEntry] = []
        for row in rows[:limit]:
            source: Source = row["source"]
            key: str = row["key"]
            publication_id = f"schedpost-{key}" if source == "scheduled" else key
            publications = (
                await self.session.scalars(
                    select(Publication)
                    .where(
                        Publication.profile_id == self.profile_id,
                        Publication.publication_id == publication_id,
                    )
                    .order_by(Publication.channel, Publication.target)
                )
            ).all()
            outcomes = [
                HistoryOutcome(
                    channel=p.channel,
                    target=p.target,
                    status=str(p.status) if str(p.status) in _KNOWN else "unknown",
                )
                for p in publications
            ]
            status = observed_status(outcomes)
            text: str | None = None
            if source == "scheduled":
                post = await self.session.scalar(
                    select(ScheduledPost).where(
                        ScheduledPost.profile_id == self.profile_id, ScheduledPost.id == int(key)
                    )
                )
                if post is None:
                    continue
                text = post.text
                lifecycle = str(post.status)
                if lifecycle == "cancelled":
                    status = "rejected" if post.reject_reason else "cancelled"
                elif lifecycle in {"scheduled", "pending_approval", "publishing"}:
                    status = lifecycle
            entries.append(
                HistoryEntry(
                    source=source,
                    key=key,
                    created_at_utc=row["stamp"] + "Z",
                    text=text,
                    status=status,
                    outcomes=outcomes,
                )
            )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = HistoryCursor(
                stamp=last["stamp"], source=last["source"], key=last["key"], filter_key=filter_key
            ).encode()
        return HistoryPage(entries=entries, next_cursor=next_cursor)
