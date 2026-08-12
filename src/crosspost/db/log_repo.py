"""LogRepository — диагностический журнал попыток публикации. Трек A, часть 4.

Каждая попытка исполнителя пишет сюда строку (profile, канал, публикация,
результат/ошибка). Для браузерных каналов при ошибке — путь к скриншоту.
Изоляция: клиентское чтение всегда фильтруется по profile_id.

Кросс-профильные методы (list_across_profiles / problem_summary / get) — для
АДМИН-консоли ошибок (Трек C): оператор видит проблемы всех клиентов сразу.
Доступ к ним ограничивается на роут-слое зависимостью require_admin (не здесь).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import Log, Profile


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
        """Последние записи профиля (новые сверху) — для внутренних джойнов (скриншоты)."""
        result = await self._s.execute(
            select(Log)
            .where(Log.profile_id == profile_id)
            .order_by(Log.created_at.desc(), Log.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get(self, log_id: int) -> Log | None:
        """Одна запись по id (для ленивой отдачи скриншота в админ-консоли)."""
        return await self._s.get(Log, log_id)

    async def list_across_profiles(
        self,
        *,
        levels: set[str] | None = None,
        profile_id: int | None = None,
        channel: str | None = None,
        from_time: datetime | None = None,
        before: int | None = None,
        since: int | None = None,
        limit: int = 20,
    ) -> list[tuple[Log, str]]:
        """Лента событий по ВСЕМ профилям (админ-консоль). Каждая строка + имя клиента.

        Фильтры (все опциональны, комбинируются по И):
          • levels    — множество уровней (напр. {"WARNING","ERROR"} — «только проблемы»);
          • profile_id — конкретный клиент;
          • channel   — конкретный канал;
          • from_time — период: только записи с created_at >= from_time.
        Пагинация по id (монотонный, уникальный — в отличие от created_at, который
        в пачечном логировании коллизирует посекундно и рвёт страницы):
          • since — только НОВЕЕ метки (id > since): инкремент, догрузка новых, [] если нет;
          • before — страница «старше метки» (id < before): «показать ещё»;
          • без курсора — последние `limit` записей.
        Порядок — id по убыванию (новые сверху). Возвращает (Log, profile_name).
        """
        stmt = select(Log, Profile.name).join(Profile, Profile.id == Log.profile_id)
        if levels:
            stmt = stmt.where(Log.level.in_(levels))
        if profile_id is not None:
            stmt = stmt.where(Log.profile_id == profile_id)
        if channel is not None:
            stmt = stmt.where(Log.channel == channel)
        if from_time is not None:
            stmt = stmt.where(Log.created_at >= from_time)
        if since is not None:
            stmt = stmt.where(Log.id > since)
        if before is not None:
            stmt = stmt.where(Log.id < before)
        stmt = stmt.order_by(Log.id.desc()).limit(limit)
        result = await self._s.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def problem_summary(
        self,
        *,
        levels: set[str],
        from_time: datetime | None = None,
    ) -> tuple[int, list[tuple[str, int]]]:
        """Сводка проблем: (число клиентов с проблемами, [(канал, счётчик)…]).

        Считается по тем же уровням/периоду, что и лента. Каналы — по убыванию
        частоты (какие падают чаще), строки без канала в разбивку не входят.
        """
        clients_stmt = select(func.count(func.distinct(Log.profile_id))).where(
            Log.level.in_(levels)
        )
        chan_stmt = (
            select(Log.channel, func.count())
            .where(Log.level.in_(levels), Log.channel.isnot(None))
            .group_by(Log.channel)
            .order_by(func.count().desc())
        )
        if from_time is not None:
            clients_stmt = clients_stmt.where(Log.created_at >= from_time)
            chan_stmt = chan_stmt.where(Log.created_at >= from_time)

        clients = (await self._s.execute(clients_stmt)).scalar_one()
        channels = [(row[0], row[1]) for row in (await self._s.execute(chan_stmt)).all()]
        return int(clients or 0), channels
