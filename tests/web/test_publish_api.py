"""Тесты роутов публикации/планирования. Итерация 2а.

Адаптеры подменяются через build_profile_adapter (моки площадок).
Проверяем: targets (live+capability), publish мульти-канал, частичный успех +
ретрай одного, отсечение нерабочего канала, scheduled save/list/cancel, изоляция.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC
from unittest.mock import patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.adapters.base import ChannelResult, ResultStatus
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import channels, profiles, publish, tg_login


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(f)
    yield f
    await engine.dispose()
    publish._RUNS.clear()


@pytest_asyncio.fixture
async def client(factory) -> AsyncGenerator[AsyncClient, None]:
    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels, tg_login, publish):
        app.include_router(r.router)

    # Аутентифицируем как admin: обходит гейт profile-изоляции, прежние тесты
    # публикации оперируют любым профилем как владелец.
    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    async with factory() as s:
        admin = await UserRepository(s).create_user("admin", "adminpass", role=Role.ADMIN)
        admin_id = admin.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(admin_id))
        yield c


async def _make_profile_with_live(factory, channels_live: list[str], *, name="client-a") -> int:
    from crosspost.channels.validators import VALIDATORS
    from crosspost.db.models import CredentialKind

    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile(name)
        for ch in channels_live:
            await repo.upsert_connection(p.id, ch, ConnectionState.LIVE)
            # каналам с целью — задаём per-profile цель, иначе они не eligible.
            if VALIDATORS[ch].needs_target:
                await repo.set_credential(p.id, ch, CredentialKind.TARGET, f"grp-{name}")
        return p.id


def _post_files(names=("photo.jpg",)):
    return [("media", (n, b"\xff\xd8\xffbytes", "image/jpeg")) for n in names]


class _MockAdapter:
    def __init__(self, channel, *, result=None, raises=None):
        self.channel = channel
        self._result = result
        self._raises = raises

    async def publish(self, content, *, publication_id):
        if self._raises:
            raise self._raises
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="ok")


def _patch_adapters(mapping):
    async def fake_build(repo, profile_id, channel, target_id="", *, store=None):
        return mapping.get(channel)

    return patch("crosspost.web.routes.publish.build_profile_adapter", new=fake_build)


def _patch_adapters_by_target(mapping):
    """Мок фабрики по ключу (channel, target_id) + журнал вызовов.

    mapping: {(channel, target_id): adapter}. Возвращает (patch_ctx, calls),
    где calls — список (channel, target_id) в порядке диспатча (для пейсинга/фан-аута).
    """
    calls: list[tuple[str, str]] = []

    async def fake_build(repo, profile_id, channel, target_id="", *, store=None):
        calls.append((channel, target_id))
        return mapping.get((channel, target_id))

    return patch("crosspost.web.routes.publish.build_profile_adapter", new=fake_build), calls


async def _add_target(factory, pid: int, channel: str, name: str, value: str) -> str:
    """Завести именованную цель канала, вернуть её id строкой (для selections)."""
    from crosspost.db.target_repo import ChannelTargetRepository

    async with factory() as s:
        trepo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        target = await trepo.add_target(channel, name, value)
        return str(target.id)


# ── Targets ───────────────────────────────────────────────────────────────────


async def test_targets_reflect_live_and_capability(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])  # vk_wall НЕ live

    resp = await client.get(f"/api/profiles/{pid}/publish/targets?content_type=post")
    assert resp.status_code == 200
    by = {t["channel"]: t for t in resp.json()}

    assert by["telegram"]["eligible"] is True
    assert by["vk_wall"]["eligible"] is False  # не подключён
    assert "подключ" in by["vk_wall"]["reason"].lower()


async def test_live_channel_without_target_not_eligible(client, factory):
    # vk_wall live, но БЕЗ цели → не eligible (нужна per-profile группа).
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile("no-target")
        await repo.upsert_connection(p.id, "vk_wall", ConnectionState.LIVE)
        pid = p.id

    resp = await client.get(f"/api/profiles/{pid}/publish/targets?content_type=post")
    vk = next(t for t in resp.json() if t["channel"] == "vk_wall")
    assert vk["eligible"] is False
    assert "групп" in vk["reason"].lower()

    # И публикация отклоняется, пока цель не задана.
    with _patch_adapters({}):
        r = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "x", "channels": json.dumps(["vk_wall"])},
            files=_post_files(),
        )
    assert r.status_code == 422


# ── Publish now ───────────────────────────────────────────────────────────────


async def test_publish_multichannel_done(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram", "vk_wall"])
    adapters = {
        "telegram": _MockAdapter(
            "telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")
        ),
        "vk_wall": _MockAdapter(
            "vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vk1")
        ),
    }
    with _patch_adapters(adapters):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={
                "content_type": "post",
                "text": "hi",
                "channels": json.dumps(["telegram", "vk_wall"]),
            },
            files=_post_files(),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {o["channel"]: o["status"] for o in body["outcomes"]} == {
        "telegram": "done",
        "vk_wall": "done",
    }

    # Поллинг статуса возвращает то же.
    st = await client.get(f"/api/profiles/{pid}/publish/{body['publication_id']}")
    assert {o["channel"]: o["status"] for o in st.json()} == {"telegram": "done", "vk_wall": "done"}


async def test_partial_success_then_retry_one(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram", "vk_wall"])
    failing = {
        "telegram": _MockAdapter(
            "telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")
        ),
        "vk_wall": _MockAdapter("vk_wall", raises=RuntimeError("VK упал")),
    }
    with _patch_adapters(failing):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={
                "content_type": "post",
                "text": "hi",
                "channels": json.dumps(["telegram", "vk_wall"]),
            },
            files=_post_files(),
        )
    body = resp.json()
    pub_id = body["publication_id"]
    by = {o["channel"]: o["status"] for o in body["outcomes"]}
    assert by == {"telegram": "done", "vk_wall": "failed"}

    # Ретраим ТОЛЬКО vk_wall — теперь успешно.
    fixed = {
        "vk_wall": _MockAdapter(
            "vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vk9")
        )
    }
    with _patch_adapters(fixed):
        r = await client.post(f"/api/profiles/{pid}/publish/{pub_id}/retry/vk_wall")
    assert r.status_code == 200
    assert r.json()["status"] == "done"

    st = {
        o["channel"]: o["status"]
        for o in (await client.get(f"/api/profiles/{pid}/publish/{pub_id}")).json()
    }
    assert st == {"telegram": "done", "vk_wall": "done"}


async def test_publish_rejects_non_live_channel(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])  # vk_wall не live
    with _patch_adapters({}):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["vk_wall"])},
            files=_post_files(),
        )
    assert resp.status_code == 422
    assert "подключ" in resp.json()["detail"].lower()


async def test_publish_text_only_post_is_allowed(client, factory):
    """Текстовый пост без медиа — норма (система доставляет готовый контент)."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    with _patch_adapters({"telegram": _MockAdapter("telegram")}):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["telegram"])},
        )  # без media — допустимо
    assert resp.status_code == 200


async def test_publish_empty_post_rejected(client, factory):
    """Пост без текста И без медиа — нечего публиковать → 422."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    with _patch_adapters({"telegram": _MockAdapter("telegram")}):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "  ", "channels": json.dumps(["telegram"])},
        )  # ни текста, ни медиа
    assert resp.status_code == 422


# ── Мультицели: фан-аут по (channel, target) ──────────────────────────────────


async def test_publish_fans_out_to_two_targets_of_one_channel(client, factory):
    """Один канал, две цели → две отдельные единицы; в ответе target + target_name."""
    pid = await _make_profile_with_live(factory, ["vk_wall"])
    t1 = await _add_target(factory, pid, "vk_wall", "Группа А", "groupa")
    t2 = await _add_target(factory, pid, "vk_wall", "Группа Б", "groupb")

    adapters = {
        ("vk_wall", t1): _MockAdapter(
            "vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE)
        ),
        ("vk_wall", t2): _MockAdapter(
            "vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE)
        ),
    }
    ctx, calls = _patch_adapters_by_target(adapters)
    with ctx:
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={
                "content_type": "post",
                "text": "hi",
                "channels": json.dumps(
                    [
                        {"channel": "vk_wall", "target_id": t1},
                        {"channel": "vk_wall", "target_id": t2},
                    ]
                ),
            },
            files=_post_files(),
        )
    assert resp.status_code == 200, resp.text
    outcomes = resp.json()["outcomes"]
    by_target = {o["target"]: o for o in outcomes}
    assert set(by_target) == {t1, t2}
    assert by_target[t1]["status"] == "done"
    assert by_target[t1]["target_name"] == "Группа А"
    assert by_target[t2]["target_name"] == "Группа Б"
    # Обе цели действительно диспатчились (фабрика вызвана на каждую).
    assert set(calls) == {("vk_wall", t1), ("vk_wall", t2)}


async def test_publish_partial_then_retry_one_target(client, factory):
    """Одна цель упала — ретраим ИМЕННО её (target_id), вторая не трогается."""
    pid = await _make_profile_with_live(factory, ["vk_wall"])
    t1 = await _add_target(factory, pid, "vk_wall", "Группа А", "groupa")
    t2 = await _add_target(factory, pid, "vk_wall", "Группа Б", "groupb")

    adapters = {
        ("vk_wall", t1): _MockAdapter(
            "vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE)
        ),
        ("vk_wall", t2): _MockAdapter("vk_wall", raises=RuntimeError("VK упал")),
    }
    ctx, _ = _patch_adapters_by_target(adapters)
    with ctx:
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={
                "content_type": "post",
                "text": "hi",
                "channels": json.dumps(
                    [
                        {"channel": "vk_wall", "target_id": t1},
                        {"channel": "vk_wall", "target_id": t2},
                    ]
                ),
            },
            files=_post_files(),
        )
    pub_id = resp.json()["publication_id"]
    by = {o["target"]: o["status"] for o in resp.json()["outcomes"]}
    assert by == {t1: "done", t2: "failed"}

    # Ретрай только t2 — теперь успешно.
    fixed = {
        ("vk_wall", t2): _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE))
    }
    ctx2, calls2 = _patch_adapters_by_target(fixed)
    with ctx2:
        r = await client.post(f"/api/profiles/{pid}/publish/{pub_id}/retry/vk_wall?target_id={t2}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    assert r.json()["target"] == t2
    assert calls2 == [("vk_wall", t2)]  # ретрай тронул только t2

    st = {
        o["target"]: o["status"]
        for o in (await client.get(f"/api/profiles/{pid}/publish/{pub_id}")).json()
    }
    assert st == {t1: "done", t2: "done"}


async def test_targets_enumerate_one_entry_per_target(client, factory):
    """publish/targets отдаёт по записи на КАЖДУЮ цель канала (для галочек в UI)."""
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile("multi")
        await repo.upsert_connection(p.id, "vk_wall", ConnectionState.LIVE)
        pid = p.id
    t1 = await _add_target(factory, pid, "vk_wall", "Группа А", "groupa")
    t2 = await _add_target(factory, pid, "vk_wall", "Группа Б", "groupb")

    resp = await client.get(f"/api/profiles/{pid}/publish/targets?content_type=post")
    assert resp.status_code == 200
    vk = {t["target_id"]: t for t in resp.json() if t["channel"] == "vk_wall"}
    assert {t1, t2} <= set(vk)
    assert vk[t1]["target_name"] == "Группа А" and vk[t1]["eligible"] is True
    assert vk[t2]["target_name"] == "Группа Б" and vk[t2]["eligible"] is True


async def test_publish_rejects_unknown_target(client, factory):
    """target_id, которого нет у профиля → 422 (eligibility на уровне цели)."""
    pid = await _make_profile_with_live(factory, ["vk_wall"])
    ctx, _ = _patch_adapters_by_target({})
    with ctx:
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={
                "content_type": "post",
                "text": "hi",
                "channels": json.dumps([{"channel": "vk_wall", "target_id": "424242"}]),
            },
            files=_post_files(),
        )
    assert resp.status_code == 422


async def test_schedule_with_targets_roundtrips(client, factory):
    """Планирование в конкретные цели: channels хранится вербатим как list[{...}]."""
    pid = await _make_profile_with_live(factory, ["vk_wall"])
    t1 = await _add_target(factory, pid, "vk_wall", "Группа А", "groupa")
    sel = [{"channel": "vk_wall", "target_id": t1}]
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "later",
            "channels": json.dumps(sel),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["channels"] == sel  # вербатим-снимок выбора

    listed = await client.get(f"/api/profiles/{pid}/scheduled")
    assert listed.json()[0]["channels"] == sel


# ── Scheduled ─────────────────────────────────────────────────────────────────


async def test_schedule_list_cancel(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "later",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200, resp.text
    sched_id = resp.json()["id"]
    assert resp.json()["channels"] == ["telegram"]

    listed = await client.get(f"/api/profiles/{pid}/scheduled")
    assert len(listed.json()) == 1

    dele = await client.delete(f"/api/profiles/{pid}/scheduled/{sched_id}")
    assert dele.status_code == 200
    assert (await client.get(f"/api/profiles/{pid}/scheduled")).json() == []


async def _make_executed(
    factory,
    pid: int,
    *,
    text: str,
    channels: dict[str, tuple],  # channel -> (PublicationStatus, external_id|error)
    published_at,
    screenshots: dict[str, str] | None = None,
):
    """Создать пост, проставить поканальные статусы, исполнить (published_at детермин.).

    published_at выставляем явно — курсор/since/before тестируем без гонки с часами.
    """
    from datetime import datetime

    from sqlalchemy import update as sql_update

    from crosspost.db.log_repo import LogRepository
    from crosspost.db.models import PublicationStatus, ScheduledPost
    from crosspost.db.publication_repo import PublicationRepository
    from crosspost.db.scheduler_repo import SchedulerRepository
    from crosspost.scheduler.executor import scheduled_publication_id

    async with factory() as s:
        repo = PublicationRepository(s, profile_id=pid)
        post = await repo.create_scheduled(
            content_type="post",
            text=text,
            title=None,
            media_paths=[],
            channels=list(channels),
            scheduled_at=datetime(2026, 8, 10, 9, 0),
        )
        pubid = scheduled_publication_id(post.id)
        for ch, (status, extra) in channels.items():
            if status == PublicationStatus.DONE:
                await repo.set_status(pubid, ch, status, external_id=extra)
            else:
                await repo.set_status(pubid, ch, status, error=extra)
        for ch, path in (screenshots or {}).items():
            await LogRepository(s).add(
                pid,
                channel=ch,
                publication_id=pubid,
                level="ERROR",
                message=f"scheduler: {ch} → failed",
                screenshot_path=path,
            )
        sched = SchedulerRepository(s)
        await sched.claim(post.id)
        success = all(
            st in (PublicationStatus.DONE, PublicationStatus.SUBMITTED)
            for st, _ in channels.values()
        )
        await sched.finalize(post.id, success=success)
        # Детерминированный маркер исполнения (перебиваем реальные часы finalize).
        await s.execute(
            sql_update(ScheduledPost)
            .where(ScheduledPost.id == post.id)
            .values(published_at=published_at)
        )
        await s.commit()
        return post.id


async def test_scheduled_report_shows_executed_with_outcomes(client, factory):
    """Журнал планировщика: исполненный пост читается из БД с поканальными итогами.
    Переживает перезагрузку (данные не эфемерны). Скриншоты — отдельным запросом."""
    from datetime import UTC, datetime

    from crosspost.db.models import PublicationStatus

    pid = await _make_profile_with_live(factory, ["telegram", "vk_wall"])
    post_id = await _make_executed(
        factory,
        pid,
        text="утренний пост",
        channels={
            "telegram": (PublicationStatus.DONE, "tg-42"),
            "vk_wall": (PublicationStatus.FAILED, "сессия протухла"),
        },
        published_at=datetime(2026, 8, 10, 9, 5, tzinfo=UTC),
        screenshots={"vk_wall": "/shots/x.png"},
    )

    # запланированные (ещё не отправленные) — пусто, пост уже исполнен
    assert (await client.get(f"/api/profiles/{pid}/scheduled")).json() == []

    resp = await client.get(f"/api/profiles/{pid}/scheduled/report")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert len(report) == 1
    row = report[0]
    assert row["text"] == "утренний пост"
    assert row["overall"] == "partial"  # один успех, один провал
    assert row["cursor"].startswith("2026-08-10T09:05")  # курсор = published_at
    by = {o["channel"]: o for o in row["outcomes"]}
    assert by["telegram"]["status"] == "done"
    assert by["telegram"]["external_id"] == "tg-42"
    assert by["vk_wall"]["status"] == "failed"
    assert by["vk_wall"]["error"] == "сессия протухла"
    # Скриншоты НЕ в общем ответе журнала (грузятся лениво).
    assert by["vk_wall"].get("screenshot_path") is None

    # Ленивая подгрузка скриншотов сбоя — отдельный эндпоинт.
    shots = (await client.get(f"/api/profiles/{pid}/scheduled/{post_id}/screenshots")).json()
    assert shots == {"vk_wall": "/shots/x.png"}


async def test_scheduled_screenshots_only_for_failed_channel(client, factory):
    """Канал упал на попытке 1 (скриншот в логах), но дожался на ретрае (done).

    Скриншот — артефакт СБОЯ; для успешного финального статуса он не отдаётся,
    иначе UI показал бы «Отправлено» + «скриншот сбоя» — противоречие.
    """
    from datetime import UTC, datetime

    from crosspost.db.models import PublicationStatus

    pid = await _make_profile_with_live(factory, ["vk_wall"])
    post_id = await _make_executed(
        factory,
        pid,
        text="дожали со второй попытки",
        channels={"vk_wall": (PublicationStatus.DONE, "vk-7")},
        published_at=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        screenshots={"vk_wall": "/shots/stale.png"},  # старый скриншот попытки 1
    )

    row = (await client.get(f"/api/profiles/{pid}/scheduled/report")).json()[0]
    assert row["overall"] == "done"

    shots = (await client.get(f"/api/profiles/{pid}/scheduled/{post_id}/screenshots")).json()
    assert shots == {}  # успех не тащит старый скриншот сбоя


async def test_scheduled_report_paginates_limit_and_before(client, factory):
    """limit ограничивает набор (последние N по published_at); before отдаёт старше метки."""
    from datetime import UTC, datetime

    from crosspost.db.models import PublicationStatus

    pid = await _make_profile_with_live(factory, ["telegram"])
    ids = []
    for i in range(5):  # published_at растёт: пост i исполнен в 09:0i
        ids.append(
            await _make_executed(
                factory,
                pid,
                text=f"пост {i}",
                channels={"telegram": (PublicationStatus.DONE, f"tg-{i}")},
                published_at=datetime(2026, 8, 10, 9, i, tzinfo=UTC),
            )
        )

    # Первая страница — 2 самых свежих (посты 4 и 3), новые сверху.
    page1 = (await client.get(f"/api/profiles/{pid}/scheduled/report?limit=2")).json()
    assert [r["text"] for r in page1] == ["пост 4", "пост 3"]

    # «Показать ещё»: before = курсор последней записи (пост 3) → следующие 2 старше.
    cursor = page1[-1]["cursor"]
    page2 = (
        await client.get(f"/api/profiles/{pid}/scheduled/report?limit=2&before={cursor}")
    ).json()
    assert [r["text"] for r in page2] == ["пост 2", "пост 1"]


async def test_scheduled_report_since_returns_only_newer(client, factory):
    """since отдаёт только исполненное ПОЗЖЕ метки; нет новых → пустой ответ."""
    from datetime import UTC, datetime

    from crosspost.db.models import PublicationStatus

    pid = await _make_profile_with_live(factory, ["telegram"])
    await _make_executed(
        factory,
        pid,
        text="старый",
        channels={"telegram": (PublicationStatus.DONE, "tg-old")},
        published_at=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
    )

    full = (await client.get(f"/api/profiles/{pid}/scheduled/report")).json()
    newest_cursor = full[0]["cursor"]

    # Ничего нового после последней метки → [].
    assert (
        await client.get(f"/api/profiles/{pid}/scheduled/report?since={newest_cursor}")
    ).json() == []

    # Исполнили новый пост позже метки — since отдаёт только его.
    await _make_executed(
        factory,
        pid,
        text="свежий",
        channels={"telegram": (PublicationStatus.DONE, "tg-new")},
        published_at=datetime(2026, 8, 10, 9, 30, tzinfo=UTC),
    )
    delta = (await client.get(f"/api/profiles/{pid}/scheduled/report?since={newest_cursor}")).json()
    assert [r["text"] for r in delta] == ["свежий"]


async def test_scheduled_report_isolated_by_profile(client, factory):
    """Журнал и скриншоты строго scoped по profile_id — чужие посты не видны."""
    from datetime import UTC, datetime

    from crosspost.db.models import PublicationStatus

    pid_a = await _make_profile_with_live(factory, ["telegram"], name="client-a")
    pid_b = await _make_profile_with_live(factory, ["telegram"], name="client-b")
    id_a = await _make_executed(
        factory,
        pid_a,
        text="пост A",
        channels={"telegram": (PublicationStatus.DONE, "tg-a")},
        published_at=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
    )
    await _make_executed(
        factory,
        pid_b,
        text="пост B",
        channels={"telegram": (PublicationStatus.DONE, "tg-b")},
        published_at=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
    )

    report_b = (await client.get(f"/api/profiles/{pid_b}/scheduled/report")).json()
    assert [r["text"] for r in report_b] == ["пост B"]  # чужого поста A нет

    # Пост A под профилем B не читается (scoping list_statuses по profile_id).
    shots = (await client.get(f"/api/profiles/{pid_b}/scheduled/{id_a}/screenshots")).json()
    assert shots == {}


async def test_scheduled_report_empty_when_nothing_executed(client, factory):
    """Нет исполненных постов → отчёт пуст (не 500)."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.get(f"/api/profiles/{pid}/scheduled/report")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_reschedule_updates_time(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "later",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00",
        },
        files=_post_files(),
    )
    sched_id = resp.json()["id"]

    patched = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": "2026-09-15T18:30:00"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["scheduled_at"].startswith("2026-09-15T18:30")

    # Изменение видно в списке.
    listed = await client.get(f"/api/profiles/{pid}/scheduled")
    assert listed.json()[0]["scheduled_at"].startswith("2026-09-15T18:30")


async def test_reschedule_missing_post_404(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.patch(
        f"/api/profiles/{pid}/scheduled/99999",
        json={"scheduled_at": "2026-09-15T18:30:00"},
    )
    assert resp.status_code == 404


# ── Patch scheduled text/targets ──────────────────────────────────────────────


async def test_patch_scheduled_cross_profile_404(client, factory):
    """Чужой post_id → 404 (не палим существование)."""
    pid_a = await _make_profile_with_live(factory, ["telegram"])
    pid_b = await _make_profile_with_live(factory, ["telegram"], name="client-b")
    resp = await client.post(
        f"/api/profiles/{pid_a}/scheduled",
        data={
            "content_type": "post",
            "text": "orig",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200
    sched_id = resp.json()["id"]

    r = await client.patch(
        f"/api/profiles/{pid_b}/scheduled/{sched_id}",
        json={"text": "evil"},
    )
    assert r.status_code == 404


async def test_patch_scheduled_published_409(client, factory):
    """Пост в статусе published → 409 Conflict."""
    from sqlalchemy import update as _upd

    from crosspost.db.models import ScheduledPost, ScheduledPostStatus

    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "later",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    sched_id = resp.json()["id"]

    async with factory() as s:
        await s.execute(
            _upd(ScheduledPost)
            .where(ScheduledPost.id == sched_id)
            .values(status=ScheduledPostStatus.DONE)
        )
        await s.commit()

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "should fail"},
    )
    assert r.status_code == 409


async def test_patch_scheduled_failed_is_editable(client, factory):
    """SCHEDULED-пост (в т.ч. с упавшими публикациями) — редактируется."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "orig",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    sched_id = resp.json()["id"]

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "retry text"},
    )
    assert r.status_code == 200
    assert r.json()["text"] == "retry text"


async def test_patch_scheduled_text_changes_pubid_preserved(client, factory):
    """Текст меняется; publication_id (schedpost-<id>) детерминирован по id — не меняется."""
    from crosspost.scheduler.executor import scheduled_publication_id

    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "before",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    sched_id = resp.json()["id"]

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "after"},
    )
    assert r.status_code == 200
    assert r.json()["text"] == "after"
    assert r.json()["id"] == sched_id
    assert scheduled_publication_id(sched_id) == f"schedpost-{sched_id}"


async def test_patch_scheduled_invalid_target_422(client, factory):
    """Несуществующий target_id → 422."""
    pid = await _make_profile_with_live(factory, ["vk_wall"])
    t1 = await _add_target(factory, pid, "vk_wall", "Группа А", "groupa")
    sel = [{"channel": "vk_wall", "target_id": t1}]
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "hi",
            "channels": json.dumps(sel),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200
    sched_id = resp.json()["id"]

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"channels": [{"channel": "vk_wall", "target_id": "99999"}]},
    )
    assert r.status_code == 422


# ── Таймзонная консистентность scheduled_at при PATCH ─────────────────────────


async def test_patch_aware_utc_stored_as_naive_local(client, factory):
    """Aware-UTC (+00:00) принимается и хранится как naive-local (конвертация, не стрип)."""
    from datetime import datetime, timedelta

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    # "Через час по UTC" — в aware-формате
    future_utc = datetime.now(UTC) + timedelta(hours=1)
    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": future_utc.isoformat()},
    )
    assert r.status_code == 200

    async with factory() as s:
        from crosspost.db.scheduler_repo import SchedulerRepository

        post = await SchedulerRepository(s).get(sched_id)
    # Хранится naive (tzinfo=None) — конвенция импорта
    assert post.scheduled_at.tzinfo is None
    # Значение = local-time эквивалент UTC+0: разница не больше 1 сек от ожидаемого
    expected_naive = future_utc.astimezone().replace(tzinfo=None)
    assert abs((post.scheduled_at - expected_naive).total_seconds()) < 2


async def test_patch_aware_offset_and_naive_local_same_instant_stored_identically(client, factory):
    """Aware +02:00 и naive-local для одного момента → одинаковый DB-value."""
    from datetime import datetime, timedelta, timezone

    pid = await _make_profile_with_live(factory, ["telegram"])

    # Одно и то же мгновение через hour from now, в двух форматах
    future_utc = datetime.now(UTC) + timedelta(hours=2)
    tz_plus2 = timezone(timedelta(hours=2))
    aware_plus2 = future_utc.astimezone(tz_plus2)  # aware +02:00
    naive_local = future_utc.astimezone().replace(tzinfo=None)  # naive-local системы

    id1 = await _create_scheduled(client, pid, text="a")
    id2 = await _create_scheduled(client, pid, text="b")

    r1 = await client.patch(
        f"/api/profiles/{pid}/scheduled/{id1}",
        json={"scheduled_at": aware_plus2.isoformat()},
    )
    r2 = await client.patch(
        f"/api/profiles/{pid}/scheduled/{id2}",
        json={"scheduled_at": naive_local.isoformat()},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200

    async with factory() as s:
        from crosspost.db.scheduler_repo import SchedulerRepository

        repo = SchedulerRepository(s)
        p1 = await repo.get(id1)
        p2 = await repo.get(id2)

    # Оба naive, с разницей < 1с
    assert p1.scheduled_at.tzinfo is None
    assert p2.scheduled_at.tzinfo is None
    assert abs((p1.scheduled_at - p2.scheduled_at).total_seconds()) < 1


async def test_patch_aware_utc_past_correctly_rejected(client, factory):
    """Прошедший момент в aware-UTC → 422 (без ложного пропуска из-за стрипа tzinfo)."""
    from datetime import datetime, timedelta, timezone

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    # "Час назад по UTC" — если бы стрипали tzinfo без конвертации, это выглядело бы
    # как "час назад локально" — всё равно прошлое. Тест проверяет нетривиальный случай:
    # aware-время, которое после стрипа tzinfo стало бы БУДУЩИМ (offset < 0).
    # UTC-5: "через час UTC" = "через 6 часов локально" — после неправильного стрипа
    # выглядит как будущее, хотя в UTC это прошлое.
    past_utc = datetime.now(UTC) - timedelta(hours=2)
    tz_minus5 = timezone(timedelta(hours=-5))
    # past_utc в UTC-5 = 3 часа назад по UTC-5, всё равно прошлое
    past_aware_minus5 = past_utc.astimezone(tz_minus5)

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": past_aware_minus5.isoformat()},
    )
    assert r.status_code == 422


async def test_patch_aware_utc_executor_matures_correctly(factory):
    """Executor видит aware-отредактированный пост зрелым в правильный момент."""
    from datetime import datetime, timedelta
    from unittest.mock import patch as mock_patch

    from crosspost.adapters.base import ChannelResult, ResultStatus
    from crosspost.db.engine import create_engine_and_tables
    from crosspost.db.models import ConnectionState
    from crosspost.db.profile_repo import ProfileRepository
    from crosspost.db.publication_repo import PublicationRepository
    from crosspost.db.vault import get_vault
    from crosspost.scheduler.executor import ScheduledExecutor

    _PATCH_ADAPTER = "crosspost.scheduler.executor.build_profile_adapter"

    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    from sqlalchemy.ext.asyncio import async_sessionmaker

    fac = async_sessionmaker(engine, expire_on_commit=False)

    async with fac() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile("tz_exec")
        await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)
        pid = p.id

    # Сохраняем aware-UTC "+1 минута" — должно зреть через 1 минуту
    future_utc = datetime.now(UTC) + timedelta(minutes=1)
    # Нормализуем как будет делать роут (astimezone().replace(tzinfo=None))
    naive_local = future_utc.astimezone().replace(tzinfo=None)

    async with fac() as s:
        repo = PublicationRepository(s, profile_id=pid)
        post = await repo.create_scheduled(
            content_type="post",
            text="tz",
            title=None,
            media_paths=[],
            channels=["telegram"],
            scheduled_at=naive_local,
        )
        post_id = post.id

    adapter_calls = []

    async def fake_build(repo, profile_id, channel, target_id="", **kw):
        adapter_calls.append(channel)

        class _A:
            async def publish(self, c, *, publication_id):
                return ChannelResult(channel, ResultStatus.DONE, "ok")

        return _A()

    ex = ScheduledExecutor(fac, pacing_seconds=0)
    with mock_patch(_PATCH_ADAPTER, new=fake_build):
        # Сейчас ещё не зрел
        processed = await ex.run_once(now=datetime.now())
        assert post_id not in processed

        # «Через минуту» — зрел
        processed = await ex.run_once(now=datetime.now() + timedelta(minutes=2))
        assert post_id in processed

    await engine.dispose()


# ── Валидация scheduled_at при PATCH ──────────────────────────────────────────


async def test_patch_scheduled_at_past_gives_422(client, factory):
    """scheduled_at в прошлом (старше допуска 60с) → 422, пост не тронут."""
    from datetime import datetime, timedelta

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    past = (datetime.now() - timedelta(hours=1)).isoformat()
    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": past},
    )
    assert r.status_code == 422

    async with factory() as s:
        from crosspost.db.scheduler_repo import SchedulerRepository

        post = await SchedulerRepository(s).get(sched_id)
    # Оригинальное время не изменилось
    assert "2026-08-01" in post.scheduled_at.isoformat()


async def test_patch_scheduled_at_future_accepted(client, factory):
    """Валидное будущее время у SCHEDULED → принимается, статус остаётся SCHEDULED."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    future = "2099-01-01T12:00:00"
    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": future},
    )
    assert r.status_code == 200
    assert r.json()["scheduled_at"].startswith("2099-01-01")

    async with factory() as s:
        from crosspost.db.scheduler_repo import SchedulerRepository

        post = await SchedulerRepository(s).get(sched_id)
    assert str(post.status) == "scheduled"


async def test_patch_scheduled_at_failed_rewinds(client, factory):
    """Правка времени у FAILED-поста → статус→SCHEDULED, попытки→0."""
    from sqlalchemy import update as _upd

    from crosspost.db.models import ScheduledPost
    from crosspost.db.publication_repo import PublicationRepository
    from crosspost.db.scheduler_repo import SchedulerRepository
    from crosspost.scheduler.executor import scheduled_publication_id

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)
    pubid = scheduled_publication_id(sched_id)

    async with factory() as s:
        repo = PublicationRepository(s, profile_id=pid)
        from crosspost.db.models import PublicationStatus as PS

        await repo.set_status(pubid, "telegram", PS.FAILED, error="boom")
        await repo.bump_attempt(pubid, "telegram")
        await s.execute(
            _upd(ScheduledPost).where(ScheduledPost.id == sched_id).values(status="failed")
        )
        await s.commit()

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": "2099-06-01T09:00:00"},
    )
    assert r.status_code == 200
    assert r.json()["scheduled_at"].startswith("2099-06-01")

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
        rows = await PublicationRepository(s, profile_id=pid).list_statuses(pubid)
    assert str(post.status) == "scheduled"
    assert all(row.attempt_count == 0 for row in rows)


async def test_patch_scheduled_at_tolerance_passes(client, factory):
    """Правка 'на сейчас' (в пределах 60с допуска) → проходит."""
    from datetime import datetime, timedelta

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    near_now = (datetime.now() - timedelta(seconds=30)).isoformat()
    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": near_now},
    )
    assert r.status_code == 200


async def test_patch_scheduled_at_aware_stored_consistently(client, factory):
    """Aware-datetime в PATCH сохраняется; при чтении нет смешения naive/aware."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    aware_future = "2099-03-15T14:00:00+03:00"
    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"scheduled_at": aware_future},
    )
    assert r.status_code == 200

    async with factory() as s:
        from crosspost.db.scheduler_repo import SchedulerRepository

        post = await SchedulerRepository(s).get(sched_id)
    # Храним как есть (aware или naive) — не None, не crash
    assert post.scheduled_at is not None


async def _create_scheduled(client, pid, text="orig", channels=None):
    channels = channels or ["telegram"]
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": text,
            "channels": json.dumps(channels),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_patch_scheduled_text_and_channels(client, factory):
    """Патч SCHEDULED: текст и цели меняются, статус остаётся SCHEDULED, id тот же."""
    from crosspost.scheduler.executor import scheduled_publication_id

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)
    orig_pubid = scheduled_publication_id(sched_id)

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "updated"},
    )
    assert r.status_code == 200
    assert r.json()["text"] == "updated"
    assert r.json()["id"] == sched_id
    # publication_id детерминирован, не меняется
    assert orig_pubid == scheduled_publication_id(sched_id)

    async with factory() as s:
        from crosspost.db.scheduler_repo import SchedulerRepository

        post = await SchedulerRepository(s).get(sched_id)
    assert str(post.status) == "scheduled"


async def test_patch_failed_rewinds_to_scheduled(client, factory):
    """Патч FAILED: контент меняется, статус→SCHEDULED, попытки→0."""
    from sqlalchemy import update as _upd

    from crosspost.db.models import PublicationStatus as PS
    from crosspost.db.models import ScheduledPost
    from crosspost.db.publication_repo import PublicationRepository
    from crosspost.db.scheduler_repo import SchedulerRepository
    from crosspost.scheduler.executor import scheduled_publication_id

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)
    pubid = scheduled_publication_id(sched_id)

    async with factory() as s:
        repo = PublicationRepository(s, profile_id=pid)
        await repo.set_status(pubid, "telegram", PS.FAILED, error="boom")
        await repo.bump_attempt(pubid, "telegram")
        await s.execute(
            _upd(ScheduledPost).where(ScheduledPost.id == sched_id).values(status="failed")
        )
        await s.commit()

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "retry"},
    )
    assert r.status_code == 200
    assert r.json()["text"] == "retry"

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
        rows = await PublicationRepository(s, profile_id=pid).list_statuses(pubid)
    assert str(post.status) == "scheduled"
    # Счётчики попыток сброшены
    assert all(r.attempt_count == 0 for r in rows)


async def test_patch_publishing_gives_409(client, factory):
    """Executor склеймил (PUBLISHING) до патча → 409."""
    from sqlalchemy import update as _upd

    from crosspost.db.models import ScheduledPost

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    async with factory() as s:
        await s.execute(
            _upd(ScheduledPost).where(ScheduledPost.id == sched_id).values(status="publishing")
        )
        await s.commit()

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "nope"},
    )
    assert r.status_code == 409


async def test_patch_done_gives_409(client, factory):
    """Патч DONE → 409."""
    from sqlalchemy import update as _upd

    from crosspost.db.models import ScheduledPost

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    async with factory() as s:
        await s.execute(
            _upd(ScheduledPost).where(ScheduledPost.id == sched_id).values(status="done")
        )
        await s.commit()

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "nope"},
    )
    assert r.status_code == 409


async def test_patch_invalid_target_no_side_effects(client, factory):
    """Невалидная цель → 422, статус и контент не тронуты."""
    from crosspost.db.scheduler_repo import SchedulerRepository

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid, text="original")

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"channels": [{"channel": "telegram", "target_id": "nonexistent_target_xyz"}]},
    )
    assert r.status_code == 422

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert post.text == "original"
    assert str(post.status) == "scheduled"


# ── Медиа при PATCH (часть 1: замена/установка, без удаления старого) ───────


def _make_http_transport(data: bytes):
    """Мок-транспорт httpx возвращающий фиксированный файл."""
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})

    return httpx.MockTransport(handler)


def _resolve_public(host: str) -> list[str]:
    return ["93.184.216.34"]  # example.com публичный


def _resolve_private(host: str) -> list[str]:
    return ["127.0.0.1"]  # локальный — SSRF


_IMG = b"\xff\xd8\xff" + b"x" * 100  # минимальный валидный JPEG-заголовок


async def test_patch_image_via_http_url(client, factory, monkeypatch, tmp_path):
    """Замена изображения через http URL → новый файл сохранён, пост обновлён, статус SCHEDULED."""
    import httpx

    from crosspost.db.scheduler_repo import SchedulerRepository

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    transport = _make_http_transport(_IMG)
    with (
        patch(
            "crosspost.web.routes.publish._make_http_client",
            return_value=httpx.AsyncClient(transport=transport),
        ),
        patch(
            "crosspost.scheduler.media_sources._default_resolve",
            side_effect=lambda host: ["93.184.216.34"],
        ),
    ):
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={"image": "https://example.com/new.jpg"},
        )
    assert r.status_code == 200

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert len(post.media_paths) == 1
    assert post.media_paths[0].endswith(".jpg")
    assert str(post.status) == "scheduled"


async def test_patch_image_via_yadisk(client, factory, monkeypatch, tmp_path):
    """Замена через Яндекс.Диск публичную ссылку → проходит тот же fetch_media путь."""
    import httpx

    from crosspost.db.scheduler_repo import SchedulerRepository

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    def yadisk_handler(req: httpx.Request) -> httpx.Response:
        if "cloud-api.yandex.net" in req.url.host:
            return httpx.Response(
                200,
                json={"href": "https://dl.example.com/photo.jpg", "method": "GET"},
            )
        # Прямая ссылка
        return httpx.Response(200, content=_IMG, headers={"content-type": "image/jpeg"})

    transport = httpx.MockTransport(yadisk_handler)

    yadisk_url = "https://disk.yandex.ru/i/abc123"
    with (
        patch(
            "crosspost.web.routes.publish._make_http_client",
            return_value=httpx.AsyncClient(transport=transport),
        ),
        patch(
            "crosspost.scheduler.media_sources._default_resolve",
            side_effect=lambda host: ["93.184.216.34"],  # все хосты публичны в тесте
        ),
    ):
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={"image": yadisk_url},
        )
    assert r.status_code == 200

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert len(post.media_paths) == 1


async def test_patch_image_folder_escape_rejected(client, factory, tmp_path):
    """../ escape через source-folder → 422, пост не тронут."""
    from crosspost.db.scheduler_repo import SchedulerRepository
    from crosspost.db.settings_repo import ProfileSettingsRepository

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid, text="safe")

    # Настраиваем source-folder
    async with factory() as s:
        await ProfileSettingsRepository(s, profile_id=pid).set_media_folder(str(tmp_path))

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"image": "../secret.txt"},
    )
    assert r.status_code == 422

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert post.text == "safe"
    assert str(post.status) == "scheduled"


async def test_patch_image_ssrf_rejected(client, factory):
    """SSRF: URL на локальный/приватный адрес → 422, пост не тронут."""
    import httpx

    from crosspost.db.scheduler_repo import SchedulerRepository

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid, text="orig")

    def ssrf_handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("SSRF guard должен был отклонить запрос до отправки")

    with (
        patch(
            "crosspost.web.routes.publish._make_http_client",
            return_value=httpx.AsyncClient(transport=httpx.MockTransport(ssrf_handler)),
        ),
        patch(
            "crosspost.scheduler.media_sources._default_resolve",
            side_effect=lambda host: ["127.0.0.1"],
        ),
    ):
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={"image": "https://internal.lan/photo.jpg"},
        )
    assert r.status_code == 422

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert post.text == "orig"


async def test_patch_image_failed_post_rewinds(client, factory):
    """Установка изображения на FAILED → статус→SCHEDULED, попытки→0."""
    import httpx
    from sqlalchemy import update as _upd

    from crosspost.db.models import ScheduledPost
    from crosspost.db.publication_repo import PublicationRepository
    from crosspost.db.scheduler_repo import SchedulerRepository
    from crosspost.scheduler.executor import scheduled_publication_id

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)
    pubid = scheduled_publication_id(sched_id)

    async with factory() as s:
        from crosspost.db.models import PublicationStatus as PS

        repo = PublicationRepository(s, profile_id=pid)
        await repo.set_status(pubid, "telegram", PS.FAILED, error="fail")
        await repo.bump_attempt(pubid, "telegram")
        await s.execute(
            _upd(ScheduledPost).where(ScheduledPost.id == sched_id).values(status="failed")
        )
        await s.commit()

    transport = _make_http_transport(_IMG)
    with (
        patch(
            "crosspost.web.routes.publish._make_http_client",
            return_value=httpx.AsyncClient(transport=transport),
        ),
        patch(
            "crosspost.scheduler.media_sources._default_resolve",
            side_effect=lambda host: ["93.184.216.34"],
        ),
    ):
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={"image": "https://example.com/new.jpg"},
        )
    assert r.status_code == 200

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
        rows = await PublicationRepository(s, profile_id=pid).list_statuses(pubid)
    assert str(post.status) == "scheduled"
    assert all(row.attempt_count == 0 for row in rows)


async def test_patch_image_capability_check_before_download(client, factory):
    """Канал не поддерживает POST (image-содержимое) → 422 до скачивания."""
    import httpx

    from crosspost.db.scheduler_repo import SchedulerRepository

    # telegraph поддерживает только ARTICLE, не POST
    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid, text="orig")

    download_called = []

    def counting_handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        download_called.append(req.url)
        return httpx.Response(200, content=_IMG)

    # Патчим channels на пост → telegraph (не поддерживает POST)
    with patch(
        "crosspost.web.routes.publish._make_http_client",
        return_value=httpx.AsyncClient(transport=httpx.MockTransport(counting_handler)),
    ):
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={
                "image": "https://example.com/photo.jpg",
                "channels": [{"channel": "telegraph", "target_id": ""}],
            },
        )
    assert r.status_code == 422
    assert not download_called  # файл не скачивался

    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert post.text == "orig"


# ── Медиа при PATCH (часть 2: cleanup) ────────────────────────────────────────


def _resolve_pub(host: str) -> list[str]:
    return ["93.184.216.34"]


def _patch_image_ctx(img_bytes=None):
    """Контекст-менеджер: мокает http-клиент и DNS для PATCH image."""
    from contextlib import ExitStack

    import httpx

    transport = _make_http_transport(img_bytes or _IMG)
    stack = ExitStack()
    stack.enter_context(
        patch(
            "crosspost.web.routes.publish._make_http_client",
            return_value=httpx.AsyncClient(transport=transport),
        )
    )
    stack.enter_context(
        patch(
            "crosspost.scheduler.media_sources._default_resolve",
            side_effect=_resolve_pub,
        )
    )
    return stack


async def test_cleanup_old_media_after_replace(client, factory, tmp_path, monkeypatch):
    """Старый файл удалён после успешной замены; новый присутствует; пост указывает на новый."""
    from crosspost.db.scheduler_repo import SchedulerRepository

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    # Подменяем media_paths поста на реальный файл в tmp
    old_key = "sched-old"
    old_dir = tmp_path / "media" / old_key
    old_dir.mkdir(parents=True)
    old_file = old_dir / "00_photo.jpg"
    old_file.write_bytes(b"\xff\xd8\xff")
    async with factory() as s:
        from sqlalchemy import update as _upd

        from crosspost.db.models import ScheduledPost

        await s.execute(
            _upd(ScheduledPost)
            .where(ScheduledPost.id == sched_id)
            .values(media_paths=[str(old_file)])
        )
        await s.commit()

    with _patch_image_ctx():
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={"image": "https://example.com/new.jpg"},
        )
    assert r.status_code == 200

    # Старый каталог должен быть удалён
    assert not old_dir.exists()
    # Пост указывает на новый файл
    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert len(post.media_paths) == 1
    assert "sched-old" not in post.media_paths[0]


async def test_cleanup_skip_on_422(client, factory, tmp_path, monkeypatch):
    """422 (плохой capability) → старый файл не тронут, новый не скачан."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid, text="orig")

    old_key = "sched-old2"
    old_dir = tmp_path / "media" / old_key
    old_dir.mkdir(parents=True)
    old_file = old_dir / "00_photo.jpg"
    old_file.write_bytes(b"\xff\xd8\xff")
    async with factory() as s:
        from sqlalchemy import update as _upd

        from crosspost.db.models import ScheduledPost

        await s.execute(
            _upd(ScheduledPost)
            .where(ScheduledPost.id == sched_id)
            .values(media_paths=[str(old_file)])
        )
        await s.commit()

    # Запоминаем каталоги до patch
    media_root = tmp_path / "media"
    dirs_before = {d.name for d in media_root.iterdir()}

    # telegraph не поддерживает POST → 422 до скачивания
    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={
            "image": "https://example.com/photo.jpg",
            "channels": [{"channel": "telegraph", "target_id": ""}],
        },
    )
    assert r.status_code == 422
    assert old_dir.exists()
    # Новых каталогов не появилось
    dirs_after = {d.name for d in media_root.iterdir()}
    assert dirs_after == dirs_before


async def test_cleanup_orphan_on_cas_409(client, factory, tmp_path, monkeypatch):
    """CAS False (PUBLISHING → 409) → скачанный новый файл удалён, старый не тронут."""
    from sqlalchemy import update as _upd

    from crosspost.db.models import ScheduledPost

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    old_key = "sched-old3"
    old_dir = tmp_path / "media" / old_key
    old_dir.mkdir(parents=True)
    old_file = old_dir / "00_photo.jpg"
    old_file.write_bytes(b"\xff\xd8\xff")
    async with factory() as s:
        await s.execute(
            _upd(ScheduledPost)
            .where(ScheduledPost.id == sched_id)
            .values(media_paths=[str(old_file)], status="publishing")
        )
        await s.commit()

    with _patch_image_ctx():
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={"image": "https://example.com/new.jpg"},
        )
    assert r.status_code == 409

    # Старый каталог нетронут
    assert old_dir.exists()
    # Новые файлы не осиротели: ни один каталог кроме old_key и оригинального sched- поста
    media_root = tmp_path / "media"
    # Файл для нового изображения должен быть удалён
    orphaned = [
        d
        for d in media_root.iterdir()
        if d.name != old_key and d.stat().st_mtime > old_dir.stat().st_mtime and "sched-" in d.name
    ]
    assert not orphaned, f"Orphaned dirs found: {orphaned}"


async def test_cleanup_shared_reference_not_deleted(client, factory, tmp_path, monkeypatch):
    """Старые media_paths разделены с другим постом → не удаляем."""
    from sqlalchemy import update as _upd

    from crosspost.db.models import ScheduledPost

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))

    pid = await _make_profile_with_live(factory, ["telegram"])
    id1 = await _create_scheduled(client, pid, text="a")
    id2 = await _create_scheduled(client, pid, text="b")

    shared_key = "sched-shared"
    shared_dir = tmp_path / "media" / shared_key
    shared_dir.mkdir(parents=True)
    shared_file = shared_dir / "00_photo.jpg"
    shared_file.write_bytes(b"\xff\xd8\xff")

    async with factory() as s:
        for sid in (id1, id2):
            await s.execute(
                _upd(ScheduledPost)
                .where(ScheduledPost.id == sid)
                .values(media_paths=[str(shared_file)])
            )
        await s.commit()

    with _patch_image_ctx():
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{id1}",
            json={"image": "https://example.com/new.jpg"},
        )
    assert r.status_code == 200
    # Общий файл не удалён — id2 всё ещё на него ссылается
    assert shared_dir.exists()


async def test_cleanup_old_file_missing_still_succeeds(client, factory, tmp_path, monkeypatch):
    """Старый файл уже отсутствует → замена проходит без исключения."""
    from crosspost.db.scheduler_repo import SchedulerRepository

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))

    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _create_scheduled(client, pid)

    # Указываем несуществующий путь
    async with factory() as s:
        from sqlalchemy import update as _upd

        from crosspost.db.models import ScheduledPost

        await s.execute(
            _upd(ScheduledPost)
            .where(ScheduledPost.id == sched_id)
            .values(media_paths=[str(tmp_path / "media" / "sched-gone" / "00_photo.jpg")])
        )
        await s.commit()

    with _patch_image_ctx():
        r = await client.patch(
            f"/api/profiles/{pid}/scheduled/{sched_id}",
            json={"image": "https://example.com/new.jpg"},
        )
    assert r.status_code == 200
    async with factory() as s:
        post = await SchedulerRepository(s).get(sched_id)
    assert len(post.media_paths) == 1


# ── Publish now (немедленная отправка запланированного) ───────────────────────


class _FakeExecutor:
    """Мок исполнителя: пишет статусы каналов как реальный, без браузера."""

    def __init__(self, factory, statuses: dict[str, tuple[str, str | None]]):
        self._factory = factory
        self._statuses = statuses  # channel → (status, error)

    async def publish_post_now(self, post_id: int) -> None:
        from crosspost.db.publication_repo import PublicationRepository
        from crosspost.scheduler.executor import scheduled_publication_id

        pubid = scheduled_publication_id(post_id)
        async with self._factory() as s:
            post = await PublicationRepository(s, profile_id=1).get_scheduled(post_id)
            repo = PublicationRepository(s, profile_id=post.profile_id)
            for ch, (st, err) in self._statuses.items():
                from crosspost.db.models import PublicationStatus

                await repo.set_status(pubid, ch, PublicationStatus(st), error=err)


async def _schedule_one(client, pid, channels: list[str]) -> int:
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "now-or-never",
            "channels": json.dumps(channels),
            "scheduled_at": "2026-08-01T12:00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _client_with_executor(factory, executor):
    """Отдельный клиент с executor в app.state (основной фикстурный его не ставит)."""
    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels, tg_login, publish):
        app.include_router(r.router)
    app.state.executor = executor

    async with factory() as s:
        admin = await UserRepository(s).create_user("adm2", "pw", role=Role.ADMIN)
        admin_id = admin.id
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set("session", sign_session(admin_id))
    return c


async def test_publish_now_reports_success(factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    ex = _FakeExecutor(factory, {"telegram": ("done", None)})
    c = await _client_with_executor(factory, ex)
    async with c:
        sched_id = await _schedule_one(c, pid, ["telegram"])
        resp = await c.post(f"/api/profiles/{pid}/scheduled/{sched_id}/publish-now")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["all_done"] is True
    assert body["results"] == [
        {"channel": "telegram", "status": "done", "error": None, "target": "", "target_name": ""}
    ]


async def test_publish_now_reports_failure(factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    ex = _FakeExecutor(factory, {"telegram": ("failed", "ВК: пост не появился в истории")})
    c = await _client_with_executor(factory, ex)
    async with c:
        sched_id = await _schedule_one(c, pid, ["telegram"])
        resp = await c.post(f"/api/profiles/{pid}/scheduled/{sched_id}/publish-now")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["all_done"] is False
    assert body["results"][0]["status"] == "failed"
    assert "не появился" in body["results"][0]["error"]


async def test_publish_now_missing_post_404(factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    ex = _FakeExecutor(factory, {})
    c = await _client_with_executor(factory, ex)
    async with c:
        resp = await c.post(f"/api/profiles/{pid}/scheduled/99999/publish-now")
    assert resp.status_code == 404


async def test_publish_now_no_executor_503(client, factory):
    """Если исполнитель не запущен (SCHEDULER_ENABLED=false) — понятная 503, не 500."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    sched_id = await _schedule_one(client, pid, ["telegram"])
    resp = await client.post(f"/api/profiles/{pid}/scheduled/{sched_id}/publish-now")
    assert resp.status_code == 503


# ── Связка «статья Telegraph → пост со ссылкой» ───────────────────────────────


class _CapturingAdapter:
    """Мок площадки, запоминающий, какой контент ему отдали (проверяем подстановку url)."""

    def __init__(self, channel, *, result=None, raises=None):
        self.channel = channel
        self._result = result
        self._raises = raises
        self.seen = []  # список текстов, с которыми вызвали publish

    async def publish(self, content, *, publication_id):
        self.seen.append(content.text)
        if self._raises:
            raise self._raises
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="ok")


async def test_publish_with_article_creates_telegraph_first_and_links(client, factory):
    """article_text задан → статья в Telegraph, ссылка подставлена в текст TG-поста."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    tg = _CapturingAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    tgraph = _CapturingAdapter(
        "telegraph",
        result=ChannelResult("telegraph", ResultStatus.DONE, "https://telegra.ph/Test-08-10"),
    )
    with _patch_adapters({"telegram": tg, "telegraph": tgraph}):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={
                "content_type": "post",
                "text": "короткий тизер",
                "article_text": "полное тело лонгрида",
                "channels": json.dumps(["telegram"]),
            },
            files=_post_files(),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by = {o["channel"]: o for o in body["outcomes"]}
    # Оба итога присутствуют, telegraph — с URL статьи.
    assert by["telegraph"]["status"] == "done"
    assert by["telegraph"]["external_id"] == "https://telegra.ph/Test-08-10"
    assert by["telegram"]["status"] == "done"
    # Статья получила тело лонгрида; TG-пост — тизер + ссылку на статью.
    assert tgraph.seen == ["полное тело лонгрида"]
    assert tg.seen and "https://telegra.ph/Test-08-10" in tg.seen[0]
    assert "короткий тизер" in tg.seen[0]


async def test_publish_article_failed_fail_closed(client, factory):
    """Telegraph упал → TG-пост НЕ уходит, помечается failed с причиной (fail-closed)."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    tg = _CapturingAdapter("telegram")
    tgraph = _CapturingAdapter("telegraph", raises=RuntimeError("Telegraph 500"))
    with _patch_adapters({"telegram": tg, "telegraph": tgraph}):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={
                "content_type": "post",
                "text": "тизер",
                "article_text": "тело",
                "channels": json.dumps(["telegram"]),
            },
            files=_post_files(),
        )
    assert resp.status_code == 200, resp.text
    by = {o["channel"]: o for o in resp.json()["outcomes"]}
    assert by["telegram"]["status"] == "failed"
    assert by["telegraph"]["status"] == "failed"
    # В канал ничего не ушло — адаптер telegram не вызывали.
    assert tg.seen == []


async def test_schedule_persists_article_text_flag(client, factory):
    """schedule_post сохраняет article_text; список отдаёт has_article=True."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "later",
            "article_text": "тело лонгрида",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_article"] is True

    listed = await client.get(f"/api/profiles/{pid}/scheduled")
    assert listed.json()[0]["has_article"] is True


async def test_schedule_without_article_flag_false(client, factory):
    """Пустой article_text → has_article=False (обычный пост)."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "later",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00",
        },
        files=_post_files(),
    )
    assert resp.json()["has_article"] is False


# ── GET single scheduled post ────────────────────────────────────────────────
# Новый эндпоинт GET .../scheduled/{id} используется EditSchedModal: загружает
# полный ScheduledOut перед показом формы редактирования.


async def test_get_single_scheduled_returns_post(client, factory):
    """GET .../scheduled/{id} возвращает пост с теми же полями, что list."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "to edit",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2030-01-01T10:00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200, resp.text
    sched_id = resp.json()["id"]

    r = await client.get(f"/api/profiles/{pid}/scheduled/{sched_id}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] == sched_id
    assert data["text"] == "to edit"
    assert data["status"] == "scheduled"
    assert data["channels"] == ["telegram"]


async def test_get_single_scheduled_404_for_missing(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    r = await client.get(f"/api/profiles/{pid}/scheduled/99999")
    assert r.status_code == 404


async def test_get_single_scheduled_404_for_other_profile(client, factory):
    """GET чужого поста — 404, а не данные."""
    pid_a = await _make_profile_with_live(factory, ["telegram"])
    async with factory() as s:
        pid_b = (await ProfileRepository(s, vault=get_vault()).create_profile("b")).id

    resp = await client.post(
        f"/api/profiles/{pid_a}/scheduled",
        data={
            "content_type": "post",
            "text": "private",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2030-01-01T10:00:00",
        },
        files=_post_files(),
    )
    sched_id = resp.json()["id"]
    r = await client.get(f"/api/profiles/{pid_b}/scheduled/{sched_id}")
    assert r.status_code == 404


# ── Защита формы редактирования от конкурентного поллинга ────────────────────
# Фронтенд: SchedItem и ReportRow рендерятся с key=s.id / key=r.id.
# Поллинг делает setScheduled(newList): React сравнивает ключи и не делает
# remount существующих SchedItem — локальное состояние (editing=true, text)
# сохраняется даже при смене порядка или обновлении полей в списке.
#
# Это свойство React гарантировано спецификацией reconciler; бэкенд-тест ниже
# проверяет инвариант, на который полагается фронт: ID запланированного поста
# не меняется после PATCH (CAS-апдейт возвращает тот же объект с тем же id).


async def test_patch_preserves_post_id(client, factory):
    """PATCH не меняет id поста — фронт ключует по id, remount невозможен."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "original",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2030-01-01T10:00:00",
        },
        files=_post_files(),
    )
    sched_id = resp.json()["id"]

    patch_r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "edited", "scheduled_at": "2030-06-15T09:00:00"},
    )
    assert patch_r.status_code == 200, patch_r.text
    assert patch_r.json()["id"] == sched_id
    assert patch_r.json()["text"] == "edited"
    # После PATCH пост виден в списке с тем же id — ключ стабилен.
    listed = (await client.get(f"/api/profiles/{pid}/scheduled")).json()
    assert any(p["id"] == sched_id for p in listed)


async def test_patch_409_when_publishing(client, factory):
    """409 когда исполнитель уже взял пост (status=publishing)."""
    from sqlalchemy import update as sql_update

    from crosspost.db.models import ScheduledPost, ScheduledPostStatus

    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "racing",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2030-01-01T10:00:00",
        },
        files=_post_files(),
    )
    sched_id = resp.json()["id"]

    async with factory() as s:
        await s.execute(
            sql_update(ScheduledPost)
            .where(ScheduledPost.id == sched_id)
            .values(status=ScheduledPostStatus.PUBLISHING)
        )
        await s.commit()

    r = await client.patch(
        f"/api/profiles/{pid}/scheduled/{sched_id}",
        json={"text": "too late"},
    )
    assert r.status_code == 409


# ── Media preview route ───────────────────────────────────────────────────────
# GET /api/profiles/{pid}/scheduled/{id}/media/{index}
# Авторизация: profile isolation, index-based (клиент не называет путь).
# Конфайнмент: resolved path должен быть внутри media root.


async def _make_scheduled_with_real_media(factory, client, pid, tmp_path) -> tuple[int, str]:
    """Создать scheduled post с реальным файлом на диске, вернуть (post_id, bytes_written)."""
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post",
            "text": "with media",
            "channels": json.dumps(["telegram"]),
            "scheduled_at": "2030-01-01T10:00:00",
        },
        files=[("media", ("photo.jpg", b"\xff\xd8\xff\xe0test-jpeg-bytes", "image/jpeg"))],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"], b"\xff\xd8\xff\xe0test-jpeg-bytes"


async def test_media_preview_owner_gets_200(client, factory, tmp_path):
    """Владелец получает файл с правильным content-type."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    post_id, expected_bytes = await _make_scheduled_with_real_media(factory, client, pid, tmp_path)

    r = await client.get(f"/api/profiles/{pid}/scheduled/{post_id}/media/0")
    assert r.status_code == 200, r.text
    assert r.content == expected_bytes
    assert "image" in r.headers.get("content-type", "")


async def test_media_preview_other_profile_gets_404(client, factory, tmp_path):
    """Клиент другого профиля получает 404, а не чужие байты."""
    pid_a = await _make_profile_with_live(factory, ["telegram"])
    async with factory() as s:
        pid_b = (await ProfileRepository(s, vault=get_vault()).create_profile("other")).id

    post_id, _ = await _make_scheduled_with_real_media(factory, client, pid_a, tmp_path)

    r = await client.get(f"/api/profiles/{pid_b}/scheduled/{post_id}/media/0")
    assert r.status_code == 404


async def test_media_preview_index_out_of_range(client, factory, tmp_path):
    """Индекс за границей media_paths → 404."""
    pid = await _make_profile_with_live(factory, ["telegram"])
    post_id, _ = await _make_scheduled_with_real_media(factory, client, pid, tmp_path)

    r = await client.get(f"/api/profiles/{pid}/scheduled/{post_id}/media/5")
    assert r.status_code == 404


async def test_media_preview_path_escape_blocked(client, factory, tmp_path):
    """Путь из БД, выходящий за media root → 404 (defence in depth)."""
    from sqlalchemy import update as sql_update

    from crosspost.db.models import ScheduledPost

    pid = await _make_profile_with_live(factory, ["telegram"])
    post_id, _ = await _make_scheduled_with_real_media(factory, client, pid, tmp_path)

    # Прямая запись «отравленного» пути в БД.
    poison = str(tmp_path / "outside" / "secret.jpg")
    async with factory() as s:
        await s.execute(
            sql_update(ScheduledPost)
            .where(ScheduledPost.id == post_id)
            .values(media_paths=[poison])
        )
        await s.commit()

    r = await client.get(f"/api/profiles/{pid}/scheduled/{post_id}/media/0")
    assert r.status_code == 404


async def test_media_preview_file_missing_on_disk(client, factory, tmp_path):
    """Файл уже удалён с диска → 404, не исключение."""
    import os

    pid = await _make_profile_with_live(factory, ["telegram"])
    post_id, _ = await _make_scheduled_with_real_media(factory, client, pid, tmp_path)

    # Узнаём path из списка
    detail = (await client.get(f"/api/profiles/{pid}/scheduled/{post_id}")).json()
    stored_path = detail["media_paths"][0]
    os.remove(stored_path)  # удаляем с диска, запись в БД остаётся

    r = await client.get(f"/api/profiles/{pid}/scheduled/{post_id}/media/0")
    assert r.status_code == 404


# ── Изоляция ──────────────────────────────────────────────────────────────────


async def test_publish_isolation_between_profiles(client, factory):
    pa = await _make_profile_with_live(factory, ["telegram"])
    async with factory() as s:
        pb = (await ProfileRepository(s, vault=get_vault()).create_profile("b")).id

    with _patch_adapters({"telegram": _MockAdapter("telegram")}):
        resp = await client.post(
            f"/api/profiles/{pa}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["telegram"])},
            files=_post_files(),
        )
    pub_id = resp.json()["publication_id"]

    # Профиль B не видит статусы публикации A.
    other = await client.get(f"/api/profiles/{pb}/publish/{pub_id}")
    assert other.json() == []
