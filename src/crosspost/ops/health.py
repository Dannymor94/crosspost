"""Liveness telemetry and read-only readiness checks for T-304.

No profile data, credentials, or channel identifiers leave this module.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.models import Log

logger = logging.getLogger(__name__)


@dataclass
class SchedulerProgress:
    """In-process progress signal; an alive task with no progress is not healthy."""

    clock: Callable[[], float] = time.monotonic
    consecutive_errors: int = 0
    last_progress_at: float | None = field(default=None, init=False)
    successful_ticks: int = 0

    def pulse(self) -> None:
        """Record progress inside a tick, but not proof of a completed tick."""
        self.last_progress_at = self.clock()

    def succeeded(self) -> None:
        self.consecutive_errors = 0
        self.successful_ticks += 1
        self.pulse()

    def failed(self) -> None:
        self.consecutive_errors += 1
        self.pulse()

    def healthy(self, *, max_silence_seconds: float, max_consecutive_errors: int) -> bool:
        if self.last_progress_at is None or self.successful_ticks == 0:
            return False
        age = self.clock() - self.last_progress_at
        return 0 <= age <= max_silence_seconds and self.consecutive_errors < max_consecutive_errors


@dataclass(frozen=True)
class HealthConfig:
    """Explicit and bounded health thresholds, independent of publishing policy."""

    max_silence_seconds: float = 600
    max_consecutive_errors: int = 3
    error_threshold: int = 10
    error_window_minutes: int = 15
    db_timeout_seconds: float = 3
    error_refresh_seconds: float = 60

    def __post_init__(self) -> None:
        if (
            self.max_silence_seconds <= 0
            or not math.isfinite(self.max_silence_seconds)
            or self.max_consecutive_errors < 1
            or self.error_threshold < 0
            or self.error_window_minutes < 1
            or self.db_timeout_seconds <= 0
            or not math.isfinite(self.db_timeout_seconds)
            or self.error_refresh_seconds <= 0
            or not math.isfinite(self.error_refresh_seconds)
        ):
            raise ValueError("Invalid health check configuration")

    @classmethod
    def from_env(cls) -> HealthConfig:
        return cls(
            max_silence_seconds=float(os.environ.get("HEALTH_SCHEDULER_MAX_SILENCE_SECONDS", 600)),
            max_consecutive_errors=int(os.environ.get("HEALTH_SCHEDULER_MAX_ERRORS", 3)),
            error_threshold=int(os.environ.get("HEALTH_ERROR_THRESHOLD", 10)),
            error_window_minutes=int(os.environ.get("HEALTH_ERROR_WINDOW_MINUTES", 15)),
            db_timeout_seconds=float(os.environ.get("HEALTH_DB_TIMEOUT_SECONDS", 3)),
            error_refresh_seconds=float(os.environ.get("HEALTH_ERROR_REFRESH_SECONDS", 60)),
        )


@dataclass(frozen=True)
class HealthResult:
    ready: bool
    error_spike: bool = False


@dataclass
class ErrorSpikeCache:
    """Rate-limit diagnostic reads independently from each public readiness call.

    The database query uses an indexed (level, created_at) range and LIMIT;
    optional telemetry failures mean *degraded*, never an application restart.
    """

    clock: Callable[[], float] = time.monotonic
    last_sample_at: float | None = None
    last_spike: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def observe(
        self,
        factory: async_sessionmaker[AsyncSession],
        config: HealthConfig,
        now: Callable[[], datetime],
    ) -> bool:
        current = self.clock()
        if self.last_sample_at is not None and (
            0 <= current - self.last_sample_at < config.error_refresh_seconds
        ):
            return self.last_spike
        # Do not let simultaneous unauthenticated probes queue scans.
        if self.lock.locked():
            return self.last_spike
        async with self.lock:
            current = self.clock()
            if self.last_sample_at is not None and (
                0 <= current - self.last_sample_at < config.error_refresh_seconds
            ):
                return self.last_spike
            try:
                since = now() - timedelta(minutes=config.error_window_minutes)
                limited_errors = (
                    select(Log.id)
                    .where(Log.level == "ERROR", Log.created_at >= since)
                    .limit(config.error_threshold)
                    .subquery()
                )
                async with asyncio.timeout(min(config.db_timeout_seconds, 1.0)):
                    async with factory() as session:
                        found = await session.scalar(
                            select(func.count()).select_from(limited_errors)
                        )
                self.last_spike = int(found or 0) >= config.error_threshold
            except Exception:  # noqa: BLE001 — telemetry must not break core readiness
                logger.warning("health diagnostic sampling failed")
                self.last_spike = True  # fail visibly degraded, not falsely healthy
            self.last_sample_at = self.clock()
            return self.last_spike


def _scheduler_healthy(
    enabled: bool | None,
    task: asyncio.Task[object] | None,
    progress: SchedulerProgress | None,
    config: HealthConfig,
) -> bool:
    return (
        enabled is True
        and task is not None
        and not task.done()
        and progress is not None
        and progress.healthy(
            max_silence_seconds=config.max_silence_seconds,
            max_consecutive_errors=config.max_consecutive_errors,
        )
    )


async def evaluate_readiness(
    factory: async_sessionmaker[AsyncSession] | None,
    *,
    scheduler_enabled: bool | None,
    scheduler_task: asyncio.Task[object] | None,
    progress: SchedulerProgress | None,
    config: HealthConfig,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    error_cache: ErrorSpikeCache | None = None,
) -> HealthResult:
    """Fail-closed DB + scheduler readiness, with nonfatal aggregate error signal.

    Error spikes are reported as a distinct flag: a platform outage must not
    turn an otherwise healthy publishing process into a restart loop.
    """
    if factory is None or not _scheduler_healthy(
        scheduler_enabled, scheduler_task, progress, config
    ):
        return HealthResult(ready=False)

    try:
        async with asyncio.timeout(config.db_timeout_seconds):
            async with factory() as session:
                # A constant SELECT can succeed without touching the database file.
                # Probe a mandatory schema table to detect broken/missing DBs.
                await session.execute(text("SELECT 1 FROM profiles LIMIT 1"))
    except Exception:  # noqa: BLE001 — health must not expose DB error detail
        logger.warning("health DB check failed")
        return HealthResult(ready=False)

    # A scheduler may have terminated while the DB probe was waiting.
    if not _scheduler_healthy(scheduler_enabled, scheduler_task, progress, config):
        return HealthResult(ready=False)
    error_spike = False
    if config.error_threshold:
        sampler = error_cache or ErrorSpikeCache()
        error_spike = await sampler.observe(factory, config, now)
    # Diagnostic sampling can itself await; recheck after all I/O.
    if not _scheduler_healthy(scheduler_enabled, scheduler_task, progress, config):
        return HealthResult(ready=False)
    return HealthResult(ready=True, error_spike=error_spike)
