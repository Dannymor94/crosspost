"""Persistent circuit breaker on (profile_id, channel). T-203.

States: CLOSED → normal dispatch allowed.
        OPEN   → no dispatch until open_until; auto-transitions to HALF_OPEN.
        HALF_OPEN → one probe allowed; success → CLOSED, failure → OPEN again.

All datetimes are stored as naive (no tzinfo) to match browser_cooldown.py convention.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import BreakerState, CircuitBreaker


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


class CircuitBreakerRepository:
    """Async repository for circuit breaker state per (profile_id, channel)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_state(self, profile_id: int, channel: str, *, now: datetime) -> BreakerState:
        """Return current state; auto-transition OPEN→HALF_OPEN when cooldown expired."""
        from sqlalchemy import select  # noqa: PLC0415

        result = await self._s.execute(
            select(CircuitBreaker).where(
                CircuitBreaker.profile_id == profile_id,
                CircuitBreaker.channel == channel,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return BreakerState.CLOSED
        if row.state == BreakerState.OPEN:
            if row.open_until is not None and _naive(row.open_until) <= _naive(now):
                await self._s.execute(
                    sa_update(CircuitBreaker)
                    .where(
                        CircuitBreaker.id == row.id,
                        CircuitBreaker.state == BreakerState.OPEN,
                    )
                    .values(state=BreakerState.HALF_OPEN)
                )
                await self._s.commit()
                return BreakerState.HALF_OPEN
        return BreakerState(row.state)

    async def record_failure(
        self,
        profile_id: int,
        channel: str,
        *,
        now: datetime,
        threshold: int,
        cooldown_seconds: float,
    ) -> None:
        """Increment failure count; open circuit when threshold reached."""
        from sqlalchemy import select  # noqa: PLC0415

        result = await self._s.execute(
            select(CircuitBreaker).where(
                CircuitBreaker.profile_id == profile_id,
                CircuitBreaker.channel == channel,
            )
        )
        row = result.scalar_one_or_none()
        new_count = (row.failure_count if row else 0) + 1
        now_naive = _naive(now)
        if new_count >= threshold:
            new_state = BreakerState.OPEN
            open_until: datetime | None = now_naive + timedelta(seconds=cooldown_seconds)
        else:
            new_state = BreakerState(row.state) if row else BreakerState.CLOSED
            open_until = row.open_until if row else None

        stmt = (
            sqlite_insert(CircuitBreaker)
            .values(
                profile_id=profile_id,
                channel=channel,
                state=new_state,
                failure_count=new_count,
                open_until=open_until,
                updated_at=now_naive,
            )
            .on_conflict_do_update(
                index_elements=["profile_id", "channel"],
                set_={
                    "state": new_state,
                    "failure_count": new_count,
                    "open_until": open_until,
                    "updated_at": now_naive,
                },
            )
        )
        await self._s.execute(stmt)
        await self._s.commit()

    async def record_success(self, profile_id: int, channel: str) -> None:
        """Reset circuit to CLOSED on success. No-op if no row exists."""
        await self._s.execute(
            sa_update(CircuitBreaker)
            .where(
                CircuitBreaker.profile_id == profile_id,
                CircuitBreaker.channel == channel,
            )
            .values(state=BreakerState.CLOSED, failure_count=0, open_until=None)
        )
        await self._s.commit()

    async def try_acquire_half_open(
        self, profile_id: int, channel: str, *, now: datetime, cooldown_seconds: float
    ) -> bool:
        """Atomically acquire the HALF_OPEN probe slot: HALF_OPEN → OPEN.

        Returns True if the caller gets the probe. The re-opened window ensures
        a failed probe doesn't allow immediate retry by another worker.
        """
        open_until = _naive(now) + timedelta(seconds=cooldown_seconds)
        result: CursorResult = await self._s.execute(  # type: ignore[assignment]
            sa_update(CircuitBreaker)
            .where(
                CircuitBreaker.profile_id == profile_id,
                CircuitBreaker.channel == channel,
                CircuitBreaker.state == BreakerState.HALF_OPEN,
            )
            .values(state=BreakerState.OPEN, open_until=open_until)
        )
        await self._s.commit()
        return result.rowcount > 0
