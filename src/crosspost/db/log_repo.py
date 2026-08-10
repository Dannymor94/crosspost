"""LogRepository — диагностический журнал попыток публикации. Трек A, часть 4.

Каждая попытка исполнителя пишет сюда строку (profile, канал, публикация,
результат/ошибка). Для браузерных каналов при ошибке — путь к скриншоту.
Изоляция: чтение всегда фильтруется по profile_id; запись привязана к нему.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import Log


class LogRepository:
    """Async-репозиторий диагностических записей."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        profile_id: int,
        *,
        channel: str | None = None,
        publication_id: str | None = None,
        level: str = "INFO",
        message: str = "",
        screenshot_path: str | None = None,
    ) -> Log:
        """Записать диагностическую строку. Секреты сюда НЕ пишем (только факты)."""
        entry = Log(
            profile_id=profile_id,
            channel=channel,
            publication_id=publication_id,
            level=level,
            message=message,
            screenshot_path=screenshot_path,
        )
        self._s.add(entry)
        await self._s.commit()
        await self._s.refresh(entry)
        return entry

    async def list_for_profile(self, profile_id: int, *, limit: int = 200) -> list[Log]:
        """Последние записи профиля (новые сверху) — для админской консоли ошибок."""
        result = await self._s.execute(
            select(Log)
            .where(Log.profile_id == profile_id)
            .order_by(Log.created_at.desc(), Log.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
