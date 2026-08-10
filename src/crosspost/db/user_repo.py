"""UserRepository — доступ к аккаунтам панели (admin/client).

Отдельно от ProfileRepository: у пользователей своя таблица и НЕТ per-profile
изоляции на уровне репозитория (аутентификация происходит до выбора профиля).
Разграничение доступа к профилям — на уровне роутов (deps.require_profile_access).

Пароль в репозиторий приходит СЫРЫМ ровно на время create_user и тут же
превращается в необратимый хэш (scrypt). Сырой пароль никуда не сохраняется.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.auth.passwords import hash_password
from crosspost.db.models import Role, User


class UserRepository:
    """Async-репозиторий пользователей панели."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_user(
        self,
        login: str,
        password: str,
        *,
        role: Role,
        profile_id: int | None = None,
    ) -> User:
        """Создать пользователя. Пароль хэшируется здесь; сырой не хранится.

        Бросает IntegrityError при дублирующемся login (unique).
        """
        user = User(
            login=login,
            password_hash=hash_password(password),
            role=role,
            profile_id=profile_id,
        )
        self._s.add(user)
        await self._s.commit()
        await self._s.refresh(user)
        return user

    async def get_by_id(self, user_id: int) -> User | None:
        return await self._s.get(User, user_id)

    async def get_by_login(self, login: str) -> User | None:
        result = await self._s.execute(select(User).where(User.login == login))
        return result.scalar_one_or_none()

    async def list_clients(self) -> list[User]:
        """Все client-аккаунты (для админского списка), по возрастанию id."""
        result = await self._s.execute(
            select(User).where(User.role == Role.CLIENT).order_by(User.id)
        )
        return list(result.scalars().all())

    async def count(self) -> int:
        """Сколько всего пользователей (для bootstrap: пусто → нужен первый admin)."""
        result = await self._s.execute(select(func.count()).select_from(User))
        return int(result.scalar_one())
