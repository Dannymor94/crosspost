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
    last_progress_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.last_progress_at = self.clock()

    def pulse(self) -> None:
        self.last_progress_at = self.clock()

    def succeeded(self) -> None:
        self.consecutive_errors = 0
        self.pulse()

    def failed(self) -> None:
        self.consecutive_errors += 1
        self.pulse()

    def healthy(self, *, max_silence_seconds: float, max_consecutive_errors: int) -> bool:
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

    def __post_init__(self) -> None:
        if (
            self.max_silence_seconds <= 0
            or not math.isfinite(self.max_silence_seconds)
            or self.max_consecutive_errors < 1
            or self.error_threshold < 0
            or self.error_window_minutes < 1
            or self.db_timeout_seconds <= 0
            or not math.isfinite(self.db_timeout_seconds)
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
        )


@dataclass(frozen=True)
class HealthResult:
    ready: bool
    error_spike: bool = False


async def evaluate_readiness(
    factory: async_sessionmaker[AsyncSession] | None,
    *,
    scheduler_enabled: bool | None,
    scheduler_task: asyncio.Task[object] | None,
    progress: SchedulerProgress | None,
    config: HealthConfig,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> HealthResult:
    """Fail-closed DB + scheduler readiness, with nonfatal aggregate error signal.

    Error spikes are reported as a distinct flag: a platform outage must not
    turn an otherwise healthy publishing process into a restart loop.
    """
    if (
        factory is None
        or scheduler_enabled is not True
        or scheduler_task is None
        or scheduler_task.done()
        or progress is None
        or not progress.healthy(
            max_silence_seconds=config.max_silence_seconds,
            max_consecutive_errors=config.max_consecutive_errors,
        )
    ):
        return HealthResult(ready=False)

    try:
        async with asyncio.timeout(config.db_timeout_seconds):
            async with factory() as session:
                # A constant SELECT can succeed without touching the database file.
                # Probe a mandatory schema table to detect broken/missing DBs.
                await session.execute(text("SELECT 1 FROM profiles LIMIT 1"))
                error_spike = False
                if config.error_threshold:
                    since = now() - timedelta(minutes=config.error_window_minutes)
                    error_count = await session.scalar(
                        select(func.count(Log.id)).where(
                            Log.level == "ERROR", Log.created_at >= since
                        )
                    )
                    error_spike = int(error_count or 0) >= config.error_threshold
    except (Exception, TimeoutError):  # noqa: BLE001 — health must not expose DB error detail
        logger.warning("health DB check failed")
        return HealthResult(ready=False)

    return HealthResult(ready=True, error_spike=error_spike)
