"""PublicationRepository — статусы поканальных публикаций + запланированные посты.

Итерация 2а. Всё ВСЕГДА фильтруется по profile_id (изоляция).

Две области:
  1. Поканальный статус публикации (publications): attempting → done/failed/submitted.
     Ключ идемпотентности — (profile_id, publication_id, channel).
  2. Запланированные посты (scheduled_posts): снимок контента + каналы + время.

Оркестратор пишет статусы сюда; UI поллит list_statuses для «живой» картины.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import delete, select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import (
    SCHEDULED_TERMINAL,
    Publication,
    PublicationStatus,
    ScheduledPost,
    ScheduledPostStatus,
)


class PublicationRepository:
    """Async-репозиторий публикаций/расписания с изоляцией по profile_id."""

    def __init__(self, session: AsyncSession, *, profile_id: int) -> None:
        self._s = session
        self._pid = profile_id

    # ── Поканальный статус ────────────────────────────────────────────────────

    async def set_status(
        self,
        publication_id: str,
        channel: str,
        status: PublicationStatus,
        *,
        target: str = "",
        external_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Upsert поканального статуса (profile_id, publication_id, channel, target)."""
        stmt = (
            sqlite_insert(Publication)
            .values(
                profile_id=self._pid,
                publication_id=publication_id,
                channel=channel,
                target=target,
                status=status,
                external_id=external_id,
                error=error,
            )
            .on_conflict_do_update(
                index_elements=["profile_id", "publication_id", "channel", "target"],
                set_={"status": status, "external_id": external_id, "error": error},
            )
        )
        await self._s.execute(stmt)
        await self._s.commit()

    async def get_status(
        self, publication_id: str, channel: str, *, target: str = ""
    ) -> Publication | None:
        # populate_existing: upsert идёт сырым SQL мимо identity-map — читаем свежее из БД.
        result = await self._s.execute(
            select(Publication)
            .where(
                Publication.profile_id == self._pid,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.target == target,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_statuses(self, publication_id: str) -> list[Publication]:
        result = await self._s.execute(
            select(Publication)
            .where(
                Publication.profile_id == self._pid,
                Publication.publication_id == publication_id,
            )
            .order_by(Publication.channel, Publication.target)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def bump_attempt(self, publication_id: str, channel: str, *, target: str = "") -> None:
        """Инкремент счётчика попыток (профиль-scoped), помечая attempting.

        Вызывается ДО попытки — счётчик переживает падение процесса посреди
        отправки, поэтому потолок ретраев соблюдается и после перезапуска.
        Первая попытка создаёт строку (attempt_count=1); последующие +1.
        Статус/external_id из финального результата запишет set_status ниже.
        """
        stmt = (
            sqlite_insert(Publication)
            .values(
                profile_id=self._pid,
                publication_id=publication_id,
                channel=channel,
                target=target,
                status=PublicationStatus.ATTEMPTING,
                attempt_count=1,
            )
            .on_conflict_do_update(
                index_elements=["profile_id", "publication_id", "channel", "target"],
                set_={
                    "attempt_count": Publication.attempt_count + 1,
                    "status": PublicationStatus.ATTEMPTING,
                },
            )
        )
        await self._s.execute(stmt)
        await self._s.commit()

    async def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool:
        row = await self._s.execute(
            select(Publication.id).where(
                Publication.profile_id == self._pid,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.target == target,
                Publication.status.in_((PublicationStatus.DONE, PublicationStatus.SUBMITTED)),
            )
        )
        return row.first() is not None

    # ── Запланированные посты ─────────────────────────────────────────────────

    async def create_scheduled(
        self,
        *,
        content_type: str,
        text: str,
        title: str | None,
        media_paths: list[str],
        channels: list,  # list[str] (legacy) | list[{"channel","target_id"}] (мультицели)
        scheduled_at: datetime,
        article_text: str | None = None,
        link_phrase: str | None = None,
    ) -> ScheduledPost:
        post = ScheduledPost(
            profile_id=self._pid,
            content_type=content_type,
            text=text,
            title=title,
            media_paths=media_paths,
            channels=channels,
            scheduled_at=scheduled_at,
            article_text=article_text,
            link_phrase=link_phrase,
        )
        self._s.add(post)
        await self._s.commit()
        await self._s.refresh(post)
        return post

    async def list_scheduled(self) -> list[ScheduledPost]:
        """Активные (не отменённые) запланированные посты профиля, по времени."""
        result = await self._s.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.profile_id == self._pid,
                ScheduledPost.status == ScheduledPostStatus.SCHEDULED,
            )
            .order_by(ScheduledPost.scheduled_at)
        )
        return list(result.scalars().all())

    async def list_executed(
        self,
        *,
        limit: int = 20,
        before: datetime | None = None,
        since: datetime | None = None,
    ) -> list[ScheduledPost]:
        """Исполненные планировщиком посты профиля (status=published), новые сверху.

        Для «Журнала планировщика»: пост в published переходит, когда ВСЕ его
        каналы финализировались (успех ИЛИ исчерпан ретрай). Поканальные итоги —
        отдельно, в publications по ключу schedpost-<id> (см. роут отчёта).

        Курсор/инкремент — по published_at (момент исполнения, монотонный):
          • since — только исполненные ПОЗЖЕ метки (published_at > since): фронт
            догружает новые записи, не перекачивая историю. Нет новых → [].
          • before — страница «старше метки» (published_at < before): «показать ещё».
          • без курсора — последние `limit` записей.
        Индекс ix_scheduled_report (profile_id, status, published_at) покрывает запрос.
        Строки без published_at (легаси до фичи) в журнал не попадают — у них нет курсора.
        """
        stmt = select(ScheduledPost).where(
            ScheduledPost.profile_id == self._pid,
            ScheduledPost.status.in_(SCHEDULED_TERMINAL),
            ScheduledPost.published_at.isnot(None),
        )
        if since is not None:
            stmt = stmt.where(ScheduledPost.published_at > since)
        if before is not None:
            stmt = stmt.where(ScheduledPost.published_at < before)
        stmt = stmt.order_by(ScheduledPost.published_at.desc(), ScheduledPost.id.desc()).limit(
            limit
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def get_scheduled(self, scheduled_id: int) -> ScheduledPost | None:
        result = await self._s.execute(
            select(ScheduledPost).where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.profile_id == self._pid,
            )
        )
        return result.scalar_one_or_none()

    async def update_scheduled_time(self, scheduled_id: int, new_time: datetime) -> bool:
        """Сдвинуть scheduled_at запланированного поста профиля. True если нашли."""
        result = await self._s.execute(
            sql_update(ScheduledPost)
            .where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.profile_id == self._pid,
            )
            .values(scheduled_at=new_time)
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0

    async def update_scheduled_content(
        self, scheduled_id: int, *, text: str | None = None, channels: list | None = None
    ) -> bool:
        """Обновить текст и/или каналы запланированного поста. True если нашли."""
        values: dict = {}
        if text is not None:
            values["text"] = text
        if channels is not None:
            values["channels"] = channels
        if not values:
            return True
        result = await self._s.execute(
            sql_update(ScheduledPost)
            .where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.profile_id == self._pid,
            )
            .values(**values)
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0

    async def patch_scheduled_cas(
        self,
        scheduled_id: int,
        *,
        text: str | None = None,
        channels: list | None = None,
        scheduled_at: datetime | None = None,
        media_paths: list[str] | None = None,
        rewind_failed: bool = False,
    ) -> bool:
        """CAS-патч поста: UPDATE WHERE status IN ('scheduled','failed').

        rowcount==0 → пост не найден, чужой, или в PUBLISHING/DONE → caller кидает 409.
        rewind_failed=True: если текущий статус был FAILED → переводим в SCHEDULED и
        сбрасываем attempt_count по всем publication-строкам этого поста (перевзвод).
        Возвращает True если обновление прошло.
        """
        _editable = (ScheduledPostStatus.SCHEDULED.value, ScheduledPostStatus.FAILED.value)
        values: dict = {}
        if text is not None:
            values["text"] = text
        if channels is not None:
            values["channels"] = channels
        if scheduled_at is not None:
            values["scheduled_at"] = scheduled_at
        if media_paths is not None:
            values["media_paths"] = media_paths

        # Читаем текущий статус — нужно знать был ли FAILED чтобы решить о перевзводе.
        # Читаем ВНУТРИ транзакции перед апдейтом, чтобы не было TOCTOU: если между
        # чтением и апдейтом executor заклеймит пост → статус изменится и WHERE не сработает.
        # Если нечего менять (нет полей), всё равно делаем no-op UPDATE чтобы CAS сработал.
        if not values:
            values["id"] = scheduled_id  # no-op: SET id=id (текущее значение)

        stmt = (
            sql_update(ScheduledPost)
            .where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.profile_id == self._pid,
                ScheduledPost.status.in_(_editable),
            )
            .values(**values)
            .returning(ScheduledPost.status)
        )
        result = await self._s.execute(stmt)
        row = result.fetchone()
        if row is None:
            await self._s.rollback()
            return False

        prev_status = str(row[0])
        if rewind_failed and prev_status == ScheduledPostStatus.FAILED.value:
            # Перевзвод: статус → SCHEDULED, попытки → 0 по всем publication-строкам.
            await self._s.execute(
                sql_update(ScheduledPost)
                .where(ScheduledPost.id == scheduled_id)
                .values(status=ScheduledPostStatus.SCHEDULED)
            )
            from crosspost.scheduler.executor import scheduled_publication_id

            pubid = scheduled_publication_id(scheduled_id)
            await self._s.execute(
                sql_update(Publication)
                .where(
                    Publication.profile_id == self._pid,
                    Publication.publication_id == pubid,
                )
                .values(attempt_count=0)
            )

        await self._s.commit()
        return True

    async def media_key_shared(self, scheduled_id: int, media_key: str) -> bool:
        """True если хотя бы один другой scheduled_post профиля ссылается на тот же ключ каталога.

        Используется перед удалением старого медиа при PATCH: не удаляем файл,
        если он разделён с другим постом (одинаковый sched-<uuid> каталог).
        Проверяем LIKE '%/<key>/%' по JSON-строке media_paths.
        """
        result = await self._s.execute(
            select(ScheduledPost.id)
            .where(
                ScheduledPost.profile_id == self._pid,
                ScheduledPost.id != scheduled_id,
                ScheduledPost.media_paths.cast(sa.Text).contains(f"/{media_key}/"),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def cancel_scheduled(self, scheduled_id: int) -> bool:
        """Удалить запланированный пост профиля. True если что-то удалено."""
        result = await self._s.execute(
            delete(ScheduledPost).where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.profile_id == self._pid,
            )
        )
        await self._s.commit()
        # rowcount есть у CursorResult (DELETE), но session.execute типизирован как
        # Result[Any] без него — getattr держит mypy чистым в обеих средах (с/без стабов).
        return getattr(result, "rowcount", 0) > 0
