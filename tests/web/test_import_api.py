"""Тесты роутов импорта контент-плана. Трек A, часть 1.

preview — stateless (таблица + имена файлов → отчёт связывания, без БД).
commit  — таблица + картинки → ScheduledPost записи в БД.
update_scheduled_time — правка времени через репозиторий.

Канальная инфраструктура мокается через ConnectionState.LIVE в профиле.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import import_plan, profiles


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret-import")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(f)
    yield f
    await engine.dispose()


@pytest_asyncio.fixture
async def client(factory) -> AsyncGenerator[AsyncClient, None]:
    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    app.include_router(profiles.router)
    app.include_router(import_plan.router)

    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    async with factory() as s:
        admin = await UserRepository(s).create_user("admin", "adminpass", role=Role.ADMIN)
        admin_id = admin.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(admin_id))
        yield c


async def _make_profile_with_live(factory, channels_live: list[str], *, name="test") -> int:
    from crosspost.channels.validators import VALIDATORS
    from crosspost.db.models import CredentialKind

    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile(name)
        for ch in channels_live:
            await repo.upsert_connection(p.id, ch, ConnectionState.LIVE)
            if VALIDATORS[ch].needs_target:
                await repo.set_credential(p.id, ch, CredentialKind.TARGET, f"target-{name}")
        return p.id


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


# ── Preview ───────────────────────────────────────────────────────────────────


async def test_preview_links_all_rows(client, factory):
    pid = await _make_profile_with_live(factory, [], name="prev-all")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[
            ("table", ("plan.csv", _csv("файл,текст\n01.jpg,Пост 1\n02.jpg,Пост 2\n"), "text/csv"))
        ],
        data={
            "filenames": json.dumps(["01.jpg", "02.jpg"]),
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolved_count"] == 2
    assert body["problem_count"] == 0
    assert body["unmatched_files"] == []
    files_in_order = [r["file"] for r in body["resolved"]]
    assert files_in_order == ["01.jpg", "02.jpg"]
    assert all(r["source"] == "rule" for r in body["resolved"])


async def test_preview_from_sheet_url(client, factory, monkeypatch):
    """Источник таблицы — ссылка на Google Таблицу (sheet_url), не файл."""
    pid = await _make_profile_with_live(factory, [], name="prev-sheet")

    async def fake_fetch(url, *, client=None):
        assert "docs.google.com/spreadsheets" in url
        return _csv("файл,текст\n01.jpg,Из таблицы\n")

    monkeypatch.setattr(import_plan, "fetch_sheet_csv", fake_fetch)
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        data={
            "sheet_url": "https://docs.google.com/spreadsheets/d/SHEET1/edit#gid=0",
            "filenames": json.dumps(["01.jpg"]),
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolved_count"] == 1
    assert body["resolved"][0]["text"] == "Из таблицы"


async def test_preview_sheet_url_access_error(client, factory, monkeypatch):
    """Нет доступа к таблице → 422 с человекочитаемым текстом."""
    from crosspost.scheduler.remote_table import RemoteTableError

    pid = await _make_profile_with_live(factory, [], name="prev-noaccess")

    async def fake_fetch(url, *, client=None):
        raise RemoteTableError("Нет доступа к таблице. Откройте доступ по ссылке…")

    monkeypatch.setattr(import_plan, "fetch_sheet_csv", fake_fetch)
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        data={
            "sheet_url": "https://docs.google.com/spreadsheets/d/SHEET1/edit",
            "filenames": json.dumps([]),
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 422
    assert "доступ" in resp.json()["detail"].lower()


async def test_preview_rejects_both_file_and_sheet(client, factory):
    """Файл И ссылка одновременно → 422 (нужен один источник)."""
    pid = await _make_profile_with_live(factory, [], name="prev-both")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[("table", ("plan.csv", _csv("файл,текст\n01.jpg,X\n"), "text/csv"))],
        data={
            "sheet_url": "https://docs.google.com/spreadsheets/d/SHEET1/edit",
            "filenames": json.dumps(["01.jpg"]),
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 422


async def test_preview_missing_file_becomes_warning(client, factory):
    """Файл указан, но не загружен → предупреждение, пост планируется без фото."""
    pid = await _make_profile_with_live(factory, [], name="prev-miss")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[("table", ("plan.csv", _csv("файл,текст\nmissing.jpg,Пост\n"), "text/csv"))],
        data={
            "filenames": json.dumps(["other.jpg"]),
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_count"] == 1  # запланирован
    assert body["problem_count"] == 0  # не ошибка
    assert len(body["warnings"]) == 1
    assert "без фото" in body["warnings"][0]["reason"]
    assert body["unmatched_files"] == ["other.jpg"]


async def test_preview_unmatched_files_listed(client, factory):
    pid = await _make_profile_with_live(factory, [], name="prev-unm")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[("table", ("plan.csv", _csv("файл,текст\n01.jpg,Пост\n"), "text/csv"))],
        data={
            "filenames": json.dumps(["01.jpg", "orphan.jpg"]),
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    body = resp.json()
    assert body["resolved_count"] == 1
    assert "orphan.jpg" in body["unmatched_files"]


async def test_preview_bad_extension_422(client, factory):
    pid = await _make_profile_with_live(factory, [], name="prev-ext")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[("table", ("plan.txt", b"x", "text/plain"))],
        data={"filenames": json.dumps([])},
    )
    assert resp.status_code == 422
    assert "csv" in resp.json()["detail"].lower()


async def test_preview_missing_column_422(client, factory):
    pid = await _make_profile_with_live(factory, [], name="prev-col")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[("table", ("plan.csv", _csv("название,описание\ntest.jpg,текст\n"), "text/csv"))],
        data={"filenames": json.dumps([])},
    )
    assert resp.status_code == 422


async def test_preview_all_explicit_dates_no_rule_needed(client, factory):
    pid = await _make_profile_with_live(factory, [], name="prev-expl")
    csv = _csv("файл,текст,дата,время\n01.jpg,Пост,2026-12-31,14:00\n")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[("table", ("plan.csv", csv, "text/csv"))],
        data={"filenames": json.dumps(["01.jpg"])},  # без start_date/times_csv
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_count"] == 1
    assert body["resolved"][0]["source"] == "explicit"
    assert "2026-12-31" in body["resolved"][0]["scheduled_at"]
    assert "14:00" in body["resolved"][0]["scheduled_at"]


async def test_preview_natural_sort_order(client, factory):
    pid = await _make_profile_with_live(factory, [], name="prev-sort")
    csv = _csv("файл,текст\n10.jpg,Десятый\n2.jpg,Второй\n1.jpg,Первый\n")
    resp = await client.post(
        f"/api/profiles/{pid}/import/preview",
        files=[("table", ("plan.csv", csv, "text/csv"))],
        data={
            "filenames": json.dumps(["1.jpg", "2.jpg", "10.jpg"]),
            "start_date": "2026-09-01",
            "times_csv": "09:00",
        },
    )
    body = resp.json()
    # Посты должны быть раскиданы в натуральном порядке: 1.jpg → 2.jpg → 10.jpg
    files_sorted = [r["file"] for r in body["resolved"]]
    assert files_sorted == ["1.jpg", "2.jpg", "10.jpg"]


async def test_preview_profile_isolation(factory):
    """client-роль не может делать preview чужого профиля."""

    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    app.include_router(profiles.router)
    app.include_router(import_plan.router)

    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    pid_a = await _make_profile_with_live(factory, [], name="iso-a")
    pid_b = await _make_profile_with_live(factory, [], name="iso-b")

    async with factory() as s:
        client_user = await UserRepository(s).create_user(
            "client-iso", "pass123", role=Role.CLIENT, profile_id=pid_a
        )

    csv = _csv("файл,текст\n01.jpg,Пост\n")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(client_user.id))
        own = await c.post(
            f"/api/profiles/{pid_a}/import/preview",
            files=[("table", ("plan.csv", csv, "text/csv"))],
            data={"filenames": json.dumps(["01.jpg"])},
        )
        assert own.status_code == 200
        other = await c.post(
            f"/api/profiles/{pid_b}/import/preview",
            files=[("table", ("plan.csv", csv, "text/csv"))],
            data={"filenames": json.dumps(["01.jpg"])},
        )
        assert other.status_code == 403


# ── Commit ────────────────────────────────────────────────────────────────────


async def test_commit_creates_scheduled_posts(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"], name="com-ok")
    csv = _csv("файл,текст\n01.jpg,Пост 1\n02.jpg,Пост 2\n")
    resp = await client.post(
        f"/api/profiles/{pid}/import/commit",
        files=[
            ("table", ("plan.csv", csv, "text/csv")),
            ("media", ("01.jpg", b"\xff\xd8\xff", "image/jpeg")),
            ("media", ("02.jpg", b"\xff\xd8\xff", "image/jpeg")),
        ],
        data={
            "channels": json.dumps(["telegram"]),
            "content_type": "post",
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["skipped_count"] == 0

    async with factory() as s:
        posts = await PublicationRepository(s, profile_id=pid).list_scheduled()
    assert len(posts) == 2
    assert all(p.channels == ["telegram"] for p in posts)
    assert all(p.content_type == "post" for p in posts)
    # Первый пост раньше второго (натуральный порядок).
    assert posts[0].scheduled_at < posts[1].scheduled_at


async def test_commit_with_targets_roundtrips(client, factory):
    """Импорт в конкретные цели: channels хранится вербатим как list[{channel,target_id}]."""
    from crosspost.db.target_repo import ChannelTargetRepository

    pid = await _make_profile_with_live(factory, ["vk_wall"], name="com-targets")
    async with factory() as s:
        trepo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        t1 = str((await trepo.add_target("vk_wall", "Группа А", "groupa")).id)
        t2 = str((await trepo.add_target("vk_wall", "Группа Б", "groupb")).id)

    sel = [{"channel": "vk_wall", "target_id": t1}, {"channel": "vk_wall", "target_id": t2}]
    resp = await client.post(
        f"/api/profiles/{pid}/import/commit",
        files=[
            ("table", ("plan.csv", _csv("файл,текст\n01.jpg,Пост 1\n"), "text/csv")),
            ("media", ("01.jpg", b"\xff\xd8\xff", "image/jpeg")),
        ],
        data={
            "channels": json.dumps(sel),
            "content_type": "post",
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1

    async with factory() as s:
        posts = await PublicationRepository(s, profile_id=pid).list_scheduled()
    assert len(posts) == 1
    assert posts[0].channels == sel  # снимок целей сохранён вербатим


async def test_commit_rejects_unknown_target(client, factory):
    """target_id, которого нет у профиля → 422 (валидация на уровне цели)."""
    pid = await _make_profile_with_live(factory, ["vk_wall"], name="com-badtgt")
    resp = await client.post(
        f"/api/profiles/{pid}/import/commit",
        files=[("table", ("plan.csv", _csv("файл,текст\n01.jpg,Пост 1\n"), "text/csv"))],
        data={
            "channels": json.dumps([{"channel": "vk_wall", "target_id": "999999"}]),
            "content_type": "post",
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 422


async def test_commit_persists_article_text(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"], name="com-article")
    csv = _csv("файл,текст,статья\n01.jpg,Короткий,Полное тело лонгрида\n02.jpg,Только пост,\n")
    resp = await client.post(
        f"/api/profiles/{pid}/import/commit",
        files=[
            ("table", ("plan.csv", csv, "text/csv")),
            ("media", ("01.jpg", b"\xff\xd8\xff", "image/jpeg")),
            ("media", ("02.jpg", b"\xff\xd8\xff", "image/jpeg")),
        ],
        data={
            "channels": json.dumps(["telegram"]),
            "content_type": "post",
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 200, resp.text
    async with factory() as s:
        posts = await PublicationRepository(s, profile_id=pid).list_scheduled()
    by_text = {p.text: p for p in posts}
    assert by_text["Короткий"].article_text == "Полное тело лонгрида"
    # Пустая колонка → None (обычный пост), не пустая строка.
    assert by_text["Только пост"].article_text is None


async def test_commit_rejects_non_live_channel(client, factory):
    pid = await _make_profile_with_live(factory, [], name="com-noconn")
    csv = _csv("файл,текст\n01.jpg,Пост\n")
    resp = await client.post(
        f"/api/profiles/{pid}/import/commit",
        files=[
            ("table", ("plan.csv", csv, "text/csv")),
            ("media", ("01.jpg", b"\xff", "image/jpeg")),
        ],
        data={
            "channels": json.dumps(["telegram"]),
            "content_type": "post",
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 422
    assert "подключ" in resp.json()["detail"].lower()


async def test_commit_missing_media_becomes_warning(client, factory):
    """02.jpg не загружен → пост создаётся без фото (warning), не пропускается."""
    pid = await _make_profile_with_live(factory, ["telegram"], name="com-partial")
    csv = _csv("файл,текст\n01.jpg,Пост 1\n02.jpg,Пост 2\n")
    resp = await client.post(
        f"/api/profiles/{pid}/import/commit",
        files=[
            ("table", ("plan.csv", csv, "text/csv")),
            ("media", ("01.jpg", b"\xff\xd8\xff", "image/jpeg")),
            # 02.jpg намеренно не загружаем
        ],
        data={
            "channels": json.dumps(["telegram"]),
            "content_type": "post",
            "start_date": "2026-09-01",
            "times_csv": "10:00",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 2  # оба поста созданы
    assert body["skipped_count"] == 0
    assert len(body["warnings"]) == 1
    assert "без фото" in body["warnings"][0]["reason"]

    async with factory() as s:
        posts = await PublicationRepository(s, profile_id=pid).list_scheduled()
    assert len(posts) == 2


async def test_commit_explicit_dates_no_rule(client, factory):
    """Все строки с явной датой — правило не обязательно."""
    pid = await _make_profile_with_live(factory, ["telegram"], name="com-expl")
    csv = _csv("файл,текст,дата,время\n01.jpg,Пост,2026-11-01,09:00\n")
    resp = await client.post(
        f"/api/profiles/{pid}/import/commit",
        files=[
            ("table", ("plan.csv", csv, "text/csv")),
            ("media", ("01.jpg", b"\xff", "image/jpeg")),
        ],
        data={"channels": json.dumps(["telegram"]), "content_type": "post"},
        # нет start_date / times_csv
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 1

    async with factory() as s:
        posts = await PublicationRepository(s, profile_id=pid).list_scheduled()
    assert posts[0].scheduled_at == datetime(2026, 11, 1, 9, 0)


async def test_commit_profile_isolation(factory):
    """Посты создаются только в нужном профиле, не в соседнем."""

    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    app.include_router(profiles.router)
    app.include_router(import_plan.router)

    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    pid_a = await _make_profile_with_live(factory, ["telegram"], name="iso-com-a")
    pid_b = await _make_profile_with_live(factory, ["telegram"], name="iso-com-b")

    async with factory() as s:
        admin = await UserRepository(s).create_user("iso-admin", "adminpass", role=Role.ADMIN)
        admin_id = admin.id

    csv = _csv("файл,текст\n01.jpg,Пост\n")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(admin_id))
        resp = await c.post(
            f"/api/profiles/{pid_a}/import/commit",
            files=[
                ("table", ("plan.csv", csv, "text/csv")),
                ("media", ("01.jpg", b"\xff", "image/jpeg")),
            ],
            data={
                "channels": json.dumps(["telegram"]),
                "content_type": "post",
                "start_date": "2026-09-01",
                "times_csv": "10:00",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["created"] == 1

    async with factory() as s:
        posts_b = await PublicationRepository(s, profile_id=pid_b).list_scheduled()
    assert len(posts_b) == 0


# ── update_scheduled_time ─────────────────────────────────────────────────────


async def test_update_scheduled_time(factory):
    pid = await _make_profile_with_live(factory, [], name="upd-time")
    new_dt = datetime(2026, 10, 10, 12, 0)
    orig_dt = datetime(2026, 9, 1, 10, 0)

    async with factory() as s:
        repo = PublicationRepository(s, profile_id=pid)
        post = await repo.create_scheduled(
            content_type="post",
            text="test",
            title=None,
            media_paths=[],
            channels=["telegram"],
            scheduled_at=orig_dt,
        )
        post_id = post.id

    async with factory() as s:
        repo = PublicationRepository(s, profile_id=pid)
        updated = await repo.update_scheduled_time(post_id, new_dt)
        assert updated is True
        fetched = await repo.get_scheduled(post_id)
        assert fetched is not None
        assert fetched.scheduled_at == new_dt


async def test_update_scheduled_time_wrong_profile(factory):
    """update_scheduled_time с чужим profile_id → False (изоляция)."""
    pid_a = await _make_profile_with_live(factory, [], name="upd-a")
    pid_b = await _make_profile_with_live(factory, [], name="upd-b")

    async with factory() as s:
        repo_a = PublicationRepository(s, profile_id=pid_a)
        post = await repo_a.create_scheduled(
            content_type="post",
            text="test",
            title=None,
            media_paths=[],
            channels=["telegram"],
            scheduled_at=datetime(2026, 9, 1, 10, 0),
        )
        post_id = post.id

    async with factory() as s:
        repo_b = PublicationRepository(s, profile_id=pid_b)
        updated = await repo_b.update_scheduled_time(post_id, datetime(2026, 12, 1, 9, 0))
        assert updated is False

    async with factory() as s:
        repo_a = PublicationRepository(s, profile_id=pid_a)
        unchanged = await repo_a.get_scheduled(post_id)
        assert unchanged is not None
        assert unchanged.scheduled_at == datetime(2026, 9, 1, 10, 0)
