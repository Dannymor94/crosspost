"""ORM-модели SQLAlchemy для хранилища состояния. Слой 0.1.

Таблицы:
  profiles      — именованные профили (клиент/аккаунт), единица изоляции
  connections   — состояние подключения (profile × channel): live/needs_relogin/banned
  credentials   — учётки / session-файлы (blob пока plain, шифрование в 0.2)
  publications  — идемпотентный журнал публикаций (ключ: publication_id × channel)
  logs          — диагностические записи для владельца

Изоляция: каждая строка привязана к profile_id. Запросы ВСЕГДА фильтруют по нему.
Каскадное удаление: удалил profile → всё его удалилось.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ── Enums ─────────────────────────────────────────────────────────────────────


class ConnectionState(StrEnum):
    LIVE = "live"
    NEEDS_RELOGIN = "needs_relogin"
    BANNED = "banned"


class CredentialKind(StrEnum):
    API_TOKEN = "api_token"
    STORAGE_STATE = "storage_state"  # Playwright storageState JSON
    TARGET = "target"  # per-profile цель постинга (группа VK / орг Яндекса / id канала)


class PublicationStatus(StrEnum):
    NEW = "new"
    QUEUED = "queued"
    ATTEMPTING = "attempting"
    DONE = "done"
    FAILED = "failed"
    SUBMITTED = "submitted"  # ушло на модерацию (Яндекс) — не путать с done
    NEEDS_RELOGIN = "needs_relogin"  # осела в мёртвом подключении


class ScheduledPostStatus(StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    PUBLISHED = "published"  # исполнено планировщиком (следующая итерация)


# ── ORM-модели ────────────────────────────────────────────────────────────────


class Profile(Base):
    """Именованный профиль: изолированный набор данных (аккаунты, публикации, логи).

    На MVP один профиль — один пользователь. Мультипрофиль — в эпике 9.
    """

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    connections: Mapped[list[Connection]] = relationship(
        "Connection", back_populates="profile", cascade="all, delete-orphan"
    )
    credentials: Mapped[list[Credential]] = relationship(
        "Credential", back_populates="profile", cascade="all, delete-orphan"
    )
    publications: Mapped[list[Publication]] = relationship(
        "Publication", back_populates="profile", cascade="all, delete-orphan"
    )
    logs: Mapped[list[Log]] = relationship(
        "Log", back_populates="profile", cascade="all, delete-orphan"
    )


class Connection(Base):
    """Состояние подключения (profile × channel) — ОДИН раз на пару, не на публикацию.

    Релогин/бан — свойство подключения, не задачи. CLAUDE.md §Две сущности состояния.
    """

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("profile_id", "channel", name="uq_connection"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[ConnectionState] = mapped_column(
        String(32), default=ConnectionState.LIVE, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    profile: Mapped[Profile] = relationship("Profile", back_populates="connections")


class Credential(Base):
    """Учётные данные: API-токен или Playwright storageState.

    blob — пока plaintext; шифрование (AES-GCM, ключ снаружи) добавляется в слое 0.2.
    Не хранить сырые пароли — CLAUDE.md §Жёсткие правила п.4.
    """

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("profile_id", "channel", "kind", name="uq_credential"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[CredentialKind] = mapped_column(String(32), nullable=False)
    blob: Mapped[str] = mapped_column(Text, nullable=False)  # шифровать в 0.2
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    profile: Mapped[Profile] = relationship("Profile", back_populates="credentials")


class Publication(Base):
    """Идемпотентный журнал публикаций.

    Ключ уникальности: (profile_id, publication_id, channel).
    external_id — квитанция после успеха, НЕ ключ дедупликации.
    """

    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("profile_id", "publication_id", "channel", name="uq_publication"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    publication_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[PublicationStatus] = mapped_column(
        String(32), default=PublicationStatus.NEW, nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    profile: Mapped[Profile] = relationship("Profile", back_populates="publications")


class ScheduledPost(Base):
    """Запланированная публикация: контент + каналы + время. Итерация 2а.

    Хранит СНИМОК контента и список каналов, чтобы планировщик (следующая
    итерация) исполнил её позже. Медиа — пути к файлам во временном хранилище.
    Изоляция: привязка к profile_id, каскадное удаление с профилем.
    """

    __tablename__ = "scheduled_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_paths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    channels: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ScheduledPostStatus] = mapped_column(
        String(32), default=ScheduledPostStatus.SCHEDULED, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    profile: Mapped[Profile] = relationship("Profile")


class Log(Base):
    """Диагностическая запись: ошибки, скриншоты, события адаптеров.

    Хранится по (profile_id, channel, publication_id) для фильтрации владельцем.
    """

    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    profile: Mapped[Profile] = relationship("Profile", back_populates="logs")
