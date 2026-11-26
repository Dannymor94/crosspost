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
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
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
    SUBMITTED = "submitted"  # отправлено без подтверждения — не путать с done
    NEEDS_RELOGIN = "needs_relogin"  # осела в мёртвом подключении


class ScheduledPostStatus(StrEnum):
    SCHEDULED = "scheduled"  # ждёт своего времени
    PUBLISHING = "publishing"  # заклеймлен исполнителем, в полёте (краш-safe: реконсиляция)
    CANCELLED = "cancelled"
    DONE = "done"  # исполнен: все каналы терминально-успешны
    FAILED = "failed"  # исполнен: есть канал, упавший после исчерпания попыток
    PENDING_APPROVAL = "pending_approval"  # ждёт одобрения оператора (модерация клиентских постов)


# Терминальные (исполненные) статусы поста — попадают в журнал, не берутся снова.
SCHEDULED_TERMINAL = (ScheduledPostStatus.DONE, ScheduledPostStatus.FAILED)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Role(StrEnum):
    ADMIN = "admin"  # владелец: видит и оперирует ВСЕ профили
    CLIENT = "client"  # клиент: привязан к ОДНОМУ профилю, видит только его


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
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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


class ChannelTarget(Base):
    """Именованная цель публикации канала (мультицели).

    Пара (profile_id, channel) может иметь НЕСКОЛЬКО целей: напр. Telegram →
    «Канал А», «Канал Б»; ВК → несколько сообществ. Учётка/сессия при этом ОДНА
    на канал (см. Credential) — один аккаунт, много целей публикации.

    value_blob — шифртекст (Fernet, тот же vault, что и credentials): значение цели
    (screen_name VK / org_id Яндекса / @канал TG) — публичный идентификатор, но
    посадку на открытый текст не вводим, посыл «расшифровка только в памяти» держим.
    id — стабильная идентичность цели (переживает переименование): им ключуется
    идемпотентность (publications.target) и выбор при публикации.

    Индекс ix_channel_target (profile_id, channel) — НЕунивный: несколько целей
    на канал. Изоляция: привязка к profile_id, каскадное удаление с профилем.
    """

    __tablename__ = "channel_targets"
    __table_args__ = (Index("ix_channel_target", "profile_id", "channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    value_blob: Mapped[str] = mapped_column(Text, nullable=False)  # шифртекст (Fernet)
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    topic_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    peer_access_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    profile: Mapped[Profile] = relationship("Profile")


class EditorDraft(Base):
    """Private editor snapshot, scoped to the actor and profile incarnation."""

    __tablename__ = "editor_drafts"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class Publication(Base):
    """Идемпотентный журнал публикаций.

    Ключ уникальности: (profile_id, publication_id, channel, target).
    external_id — квитанция после успеха, НЕ ключ дедупликации.

    target — id цели канала (мультицели): один канал шлёт в несколько целей под
    одним publication_id, каждая цель дедупится отдельно. Пусто ("") — таргетлесс
    единица (Telegraph) и legacy-строки до фичи мультицелей.
    """

    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "publication_id", "channel", "target", name="uq_publication"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    publication_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(64), default="", nullable=False)
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


class Checkpoint(Base):
    """Промежуточные чекпоинты адаптеров (шаги многоэтапной отправки).

    Отдельно от publications: хранит шаг-ключи вида "<publication_id>#<step>"
    (например Instagram "#container", Telegram "#photo") и терминальный маркер
    done самого адаптера. Пишется через тот же async-движок, что и остальной стейт
    — без второго соединения к файлу БД.

    Ключ: (profile_id, publication_id, channel, target). Изоляция — по profile_id.
    external_id — сохранённая квитанция шага (id контейнера/сообщения), НЕ ключ дедупа.
    """

    __tablename__ = "checkpoints"

    profile_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(64), primary_key=True)
    target: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ScheduledPost(Base):
    """Запланированная публикация: контент + каналы + время. Итерация 2а.

    Хранит СНИМОК контента и список каналов, чтобы планировщик (следующая
    итерация) исполнил её позже. Медиа — пути к файлам во временном хранилище.
    Изоляция: привязка к profile_id, каскадное удаление с профилем.
    """

    __tablename__ = "scheduled_posts"
    # Джойн «Журнала планировщика»: фильтр (profile_id, status=published) + курсор/
    # сортировка по published_at. Индекс покрывает и пагинацию, и since-инкремент.
    __table_args__ = (Index("ix_scheduled_report", "profile_id", "status", "published_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Тело лонгрида для связки Telegraph→канал. Пусто/NULL → обычный пост (см.
    # orchestrator/article_flow.run_binding). Отдельно от text (короткая подпись TG).
    article_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Фраза-текст ссылки на статью (например «Подробнее»). NULL → DEFAULT_LINK_PHRASE.
    link_phrase: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_paths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    channels: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Non-null marks an explicitly supplied instant; SQLite stores naive UTC.
    scheduled_at_utc: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    status: Mapped[ScheduledPostStatus] = mapped_column(
        String(32), default=ScheduledPostStatus.SCHEDULED, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    # Момент ИСПОЛНЕНИЯ (перехода в DONE/FAILED). Проставляется один раз в finalize.
    # Монотонный маркер: сортировка/курсор журнала и since-инкремент строятся на нём,
    # а не на scheduled_at (то — плановое время, при publish-now скачет в будущее).
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    profile: Mapped[Profile] = relationship("Profile")


class User(Base):
    """Пользователь панели: admin (владелец) или client (привязан к профилю).

    Регистрации нет — аккаунты заводит админ (см. CLI `user-add` и POST /api/users).
    Пароль хранится ТОЛЬКО как необратимый хэш (scrypt, соль внутри). Сырого пароля
    в БД нет — CLAUDE.md §Жёсткие правила п.4 (не хранить сырые пароли).

    Изоляция доступа:
      role=admin  → profile_id=NULL, видит и оперирует все профили;
      role=client → profile_id указывает на ЕДИНСТВЕННЫЙ доступный профиль.
    Профиль удалён → profile_id клиента становится NULL (SET NULL): аккаунт живёт,
    но доступа ни к чему не имеет, пока админ не переназначит/не удалит.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # scrypt, НЕ пароль
    role: Mapped[Role] = mapped_column(String(16), default=Role.CLIENT, nullable=False)
    profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    profile: Mapped[Profile | None] = relationship("Profile")


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


class CircuitBreaker(Base):
    """Персистентный circuit breaker на (profile, channel). T-203.

    Состояния: closed (нормально) → open (пауза до open_until) → half_open (проба).
    failure_count — принятые rate-limit результаты; устаревшие epochs отклоняются.
    open_until — naive UTC: момент, после которого переходим в half_open.
    epoch ограждает результаты отправок; lease_until ограничивает HALF_OPEN.
    OPEN без open_until — карантин: только проверенное ручное восстановление.
    """

    __tablename__ = "circuit_breakers"
    __table_args__ = (UniqueConstraint("profile_id", "channel", name="uq_circuit_breaker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[BreakerState] = mapped_column(
        String(16), default=BreakerState.CLOSED, nullable=False
    )
    epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    profile: Mapped[Profile] = relationship("Profile")


class ProfileSetting(Base):
    """Несекретные настройки профиля вида ключ→JSON-значение.

    Хранит НЕсекретные предпочтения импорта: сопоставление колонок таблицы
    (маппинг колонка→поле, чтобы применялся автоматически в следующий раз) и
    путь к папке-источнику картинок. Это НЕ credentials — открытый JSON, без
    vault. Секреты (пароли/сессии) сюда не кладём.

    Ключ уникален в пределах профиля: (profile_id, key). Изоляция и каскадное
    удаление — по profile_id.
    """

    __tablename__ = "profile_settings"
    __table_args__ = (UniqueConstraint("profile_id", "key", name="uq_profile_setting"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    profile: Mapped[Profile] = relationship("Profile")


class PublicationOperation(Base):
    """Durable admission tombstone; never implies a confirmed publication."""

    __tablename__ = "publication_operations"

    operation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    recovery_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
