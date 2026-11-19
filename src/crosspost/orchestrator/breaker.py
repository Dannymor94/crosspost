"""Durable timed rate-limit gate. Every settlement is fenced by its dispatch epoch.

An expired probe is quarantined, never replaced on a timer: expiry cannot prove
that a remote send stopped. Explicit verified recovery may release quarantine.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.adapters.base import FailureKind
from crosspost.db.models import BreakerState, CircuitBreaker


def utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def bounded_seconds(value: object, default: float, maximum: float = 86400) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
        if math.isfinite(number) and number > 0:
            return min(maximum, max(1.0, number))
    except (ValueError, TypeError, OverflowError):
        pass
    return default


def retry_after_seconds(value: str | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (utc_naive(parsedate_to_datetime(value)) - utc_naive(now)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return bounded_seconds(seconds, 0) or None


@dataclass(frozen=True)
class BreakerPolicy:
    cooldown_seconds: float = 300
    lease_seconds: float = 600

    def __post_init__(self) -> None:
        object.__setattr__(self, "cooldown_seconds", bounded_seconds(self.cooldown_seconds, 300))
        object.__setattr__(self, "lease_seconds", bounded_seconds(self.lease_seconds, 600, 3600))

    @classmethod
    def from_env(cls) -> BreakerPolicy:
        return cls(
            bounded_seconds(os.environ.get("BREAKER_COOLDOWN_SECONDS"), 300),
            bounded_seconds(os.environ.get("BREAKER_LEASE_SECONDS"), 600, 3600),
        )


@dataclass(frozen=True)
class DispatchPermit:
    epoch: int
    probe: bool = False


class CircuitBreakerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @staticmethod
    def _key(profile_id: int, channel: str):
        return (CircuitBreaker.profile_id == profile_id, CircuitBreaker.channel == channel)

    async def get_state(self, profile_id: int, channel: str, *, now: datetime) -> BreakerState:
        """Observation only. Reading an expired OPEN must never mutate a fresh lease."""
        state = await self._s.scalar(
            select(CircuitBreaker.state).where(*self._key(profile_id, channel))
        )
        return BreakerState(state) if state else BreakerState.CLOSED

    async def acquire(
        self, profile_id: int, channel: str, *, now: datetime, policy: BreakerPolicy
    ) -> DispatchPermit | None:
        moment = utc_naive(now)
        await self._s.execute(
            insert(CircuitBreaker)
            .values(
                profile_id=profile_id,
                channel=channel,
                state=BreakerState.CLOSED,
                failure_count=0,
                epoch=0,
                updated_at=moment,
            )
            .on_conflict_do_nothing(index_elements=["profile_id", "channel"])
        )
        # Lease expiry is quarantine, not permission to overlap an unconfirmed send.
        await self._s.execute(
            update(CircuitBreaker)
            .where(
                *self._key(profile_id, channel),
                CircuitBreaker.state == BreakerState.HALF_OPEN,
                CircuitBreaker.lease_until <= moment,
            )
            .values(
                state=BreakerState.OPEN,
                open_until=None,
                lease_until=None,
                epoch=CircuitBreaker.epoch + 1,
                updated_at=moment,
            )
        )
        result = await self._s.execute(
            update(CircuitBreaker)
            .where(
                *self._key(profile_id, channel),
                CircuitBreaker.state == BreakerState.OPEN,
                CircuitBreaker.open_until.is_not(None),
                CircuitBreaker.open_until <= moment,
            )
            .values(
                state=BreakerState.HALF_OPEN,
                epoch=CircuitBreaker.epoch + 1,
                lease_until=moment + timedelta(seconds=policy.lease_seconds),
                updated_at=moment,
            )
            .returning(CircuitBreaker.epoch)
        )
        epoch = result.scalar_one_or_none()
        if epoch is not None:
            permit: DispatchPermit | None = DispatchPermit(epoch, probe=True)
        else:
            epoch = await self._s.scalar(
                select(CircuitBreaker.epoch).where(
                    *self._key(profile_id, channel), CircuitBreaker.state == BreakerState.CLOSED
                )
            )
            permit = DispatchPermit(epoch) if epoch is not None else None
        await self._s.commit()
        return permit

    async def settle(
        self,
        profile_id: int,
        channel: str,
        permit: DispatchPermit,
        *,
        now: datetime,
        policy: BreakerPolicy,
        failure: FailureKind = FailureKind.UNKNOWN,
        confirmed_done: bool = False,
        retry_after: float | None = None,
    ) -> bool:
        """False means explicitly rejected stale result; it cannot change newer state.

        Concurrent CLOSED results serialize at the first state transition. Other
        results from that old epoch are rejected, including their failure count.
        """
        moment = utc_naive(now)
        predicates = [
            *self._key(profile_id, channel),
            CircuitBreaker.epoch == permit.epoch,
            CircuitBreaker.state
            == (BreakerState.HALF_OPEN if permit.probe else BreakerState.CLOSED),
        ]
        if permit.probe:
            predicates.append(CircuitBreaker.lease_until > moment)
        values: dict = {"updated_at": moment}
        if failure == FailureKind.RATE_LIMIT or (permit.probe and not confirmed_done):
            values.update(
                state=BreakerState.OPEN,
                open_until=moment
                + timedelta(
                    seconds=max(
                        policy.cooldown_seconds,
                        bounded_seconds(retry_after, policy.cooldown_seconds),
                    )
                ),
                lease_until=None,
                epoch=CircuitBreaker.epoch + 1,
            )
            if failure == FailureKind.RATE_LIMIT:
                values["failure_count"] = CircuitBreaker.failure_count + 1
        elif permit.probe and confirmed_done:
            values.update(
                state=BreakerState.CLOSED,
                open_until=None,
                lease_until=None,
                failure_count=0,
                epoch=CircuitBreaker.epoch + 1,
            )
        result = await self._s.execute(
            update(CircuitBreaker).where(*predicates).values(**values).returning(CircuitBreaker.id)
        )
        accepted = result.scalar_one_or_none() is not None
        await self._s.commit()
        return accepted

    async def release_unused(
        self, profile_id: int, channel: str, permit: DispatchPermit, *, now: datetime
    ) -> None:
        """Release only an owned probe that never reached adapter construction."""
        if not permit.probe:
            return
        await self._s.execute(
            update(CircuitBreaker)
            .where(
                *self._key(profile_id, channel),
                CircuitBreaker.epoch == permit.epoch,
                CircuitBreaker.state == BreakerState.HALF_OPEN,
            )
            .values(
                state=BreakerState.OPEN,
                lease_until=None,
                open_until=utc_naive(now),
                epoch=CircuitBreaker.epoch + 1,
            )
        )
        await self._s.commit()

    async def recovery_epoch(self, profile_id: int, channel: str) -> int | None:
        return await self._s.scalar(
            select(CircuitBreaker.epoch).where(*self._key(profile_id, channel))
        )

    async def verified_recovery(self, profile_id: int, channel: str, epoch: int | None) -> None:
        """Only explicit successful credential/session verification calls this.

        Capture epoch BEFORE verification; a later rate limit must not be cleared.
        An active probe cannot be reset while it might still be sending.
        """
        if epoch is None:
            return
        await self._s.execute(
            update(CircuitBreaker)
            .where(
                *self._key(profile_id, channel),
                CircuitBreaker.epoch == epoch,
                CircuitBreaker.state != BreakerState.HALF_OPEN,
            )
            .values(
                state=BreakerState.CLOSED,
                open_until=None,
                lease_until=None,
                failure_count=0,
                epoch=CircuitBreaker.epoch + 1,
            )
        )
        await self._s.commit()
