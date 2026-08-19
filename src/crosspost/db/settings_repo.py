"""ProfileSettingsRepository — несекретные настройки профиля (ключ→JSON).

Хранит предпочтения импорта контент-плана:
  • import_column_mapping — сопоставление колонок таблицы полям (маппинг
    колонка→логическое поле), чтобы применялось автоматически в следующий раз;
  • import_media_folder   — путь к папке-источнику картинок (вариант «имя файла»).

Это НЕ credentials: открытый JSON, без vault. Секреты (пароли/сессии) сюда не
кладём. Всё ВСЕГДА фильтруется по profile_id (изоляция, как в остальных репо).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import ProfileSetting

_KEY_COLUMN_MAPPING = "import_column_mapping"
_KEY_MEDIA_FOLDER = "import_media_folder"


class ProfileSettingsRepository:
    """Async-репозиторий несекретных настроек профиля (ключ→JSON)."""

    def __init__(self, session: AsyncSession, *, profile_id: int) -> None:
        self._s = session
        self._pid = profile_id

    # ── Общий доступ ключ→значение ─────────────────────────────────────────────

    async def get(self, key: str) -> dict | None:
        """Значение настройки по ключу или None, если не задана."""
        result = await self._s.execute(
            select(ProfileSetting.value).where(
                ProfileSetting.profile_id == self._pid,
                ProfileSetting.key == key,
            )
        )
        row = result.first()
        return row[0] if row is not None else None

    async def set(self, key: str, value: dict) -> None:
        """Upsert значения настройки (перезапись существующего ключа)."""
        row = await self._s.execute(
            select(ProfileSetting).where(
                ProfileSetting.profile_id == self._pid,
                ProfileSetting.key == key,
            )
        )
        existing = row.scalar_one_or_none()
        if existing is None:
            self._s.add(ProfileSetting(profile_id=self._pid, key=key, value=value))
        else:
            existing.value = value
        await self._s.commit()

    # ── Типизированные хелперы импорта ─────────────────────────────────────────

    async def get_column_mapping(self) -> dict[str, str]:
        """Сохранённый маппинг колонка→поле ({} если не задан)."""
        value = await self.get(_KEY_COLUMN_MAPPING)
        return dict(value) if value else {}

    async def set_column_mapping(self, mapping: dict[str, str]) -> None:
        await self.set(_KEY_COLUMN_MAPPING, dict(mapping))

    async def get_media_folder(self) -> str:
        """Путь к папке-источнику картинок ("" если не задан)."""
        value = await self.get(_KEY_MEDIA_FOLDER)
        return str(value.get("path", "")) if value else ""

    async def set_media_folder(self, path: str) -> None:
        await self.set(_KEY_MEDIA_FOLDER, {"path": path})
