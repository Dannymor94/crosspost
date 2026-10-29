"""Instagram Reels adapter tests. Площадка — мок HTTP-слоя.

video_url protocol (Meta Content Publishing API, confirmed by live API probe 2026-10):
  1. POST {graph}/{ig_user_id}/media
        JSON: media_type=REELS, video_url=<signed public URL>, caption
        Authorization: Bearer <token>
        -> {"id": "<container_id>"}
  2. GET {graph}/{container_id}?fields=status_code,status  (poll until FINISHED)
  3. POST {graph}/{ig_user_id}/media_publish
        JSON: creation_id=<container_id>
        -> {"id": "<media_id>"}   ← квитанция

Resumable upload (upload_type=resumable) is not available for Instagram Login tokens.

Инъектируем фейковый httpx-клиент, маршрутизирующий по URL. asyncio.sleep глушим,
чтобы опрос статуса не ждал реального времени.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost.adapters.api.instagram import (
    InstagramAdapter,
    _validate_reel_file,
)
from crosspost.adapters.base import ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.orchestrator.task import InMemoryIdempotencyStore, new_publication_id

_TOKEN = "IGQV-test-token"
_IG_USER = "17841400000000000"
_CONTAINER = "18000000000000000"
_MEDIA_ID = "17999999999999999"


# ── helpers ──────────────────────────────────────────────────────────────────

_PUBLIC_BASE = "https://test-host.example.com"


def _reel(tmp_path: Path, name: str = "clip.mp4", size: int = 1024) -> CanonicalContent:
    # Place under tmp_path/media/ so _video_url() can compute a relative path.
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    vid = media / name
    header = b"\x00\x00\x00\x18ftypmp42"
    vid.write_bytes((header + b"\x00" * size)[:size])  # ровно `size` байт
    return CanonicalContent(type=ContentType.REEL, text="Подпись к рилсу", media_paths=[vid])


def _resp(payload: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    return r


class FakeHTTP:
    """Маршрутизирует post/get по URL, отдаёт заготовленные ответы.

    status_sequence — очередь ответов GET статуса (по одному на опрос); когда
    кончается, повторяется последний. Счётчики вызовов и последние kwargs — для
    ассертов идемпотентности и формы запроса.

    Не принимает rupload.facebook.com — resumable upload не поддерживается для
    Instagram Login (IGAGW) токенов; проверено зондом 2026-10.
    """

    def __init__(
        self,
        *,
        create=None,
        publish=None,
        status_sequence=None,
    ) -> None:
        self._create = create if create is not None else _resp({"id": _CONTAINER})
        self._publish = publish if publish is not None else _resp({"id": _MEDIA_ID})
        self._status_seq = list(status_sequence or [_resp({"status_code": "FINISHED"})])
        self.create_calls = 0
        self.publish_calls = 0
        self.status_calls = 0
        self.last_create_kwargs: dict | None = None
        self.last_publish_kwargs: dict | None = None
        self.last_status_kwargs: dict | None = None

    async def post(self, url, **kwargs):
        if "/media_publish" in url:
            self.publish_calls += 1
            self.last_publish_kwargs = kwargs
            return self._publish
        if url.endswith("/media"):
            self.create_calls += 1
            self.last_create_kwargs = kwargs
            return self._create
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url, **kwargs):
        self.status_calls += 1
        self.last_status_kwargs = kwargs
        idx = min(self.status_calls - 1, len(self._status_seq) - 1)
        return self._status_seq[idx]

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Set INSTAGRAM_PUBLIC_BASE_URL, MEDIA_DIR, and MEDIA_URL_SECRET for all tests."""
    monkeypatch.setenv("INSTAGRAM_PUBLIC_BASE_URL", _PUBLIC_BASE)
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-instagram-signing-secret")


def _adapter(http, store=None, **kw):
    return InstagramAdapter(
        _TOKEN,
        _IG_USER,
        store or InMemoryIdempotencyStore(),
        client=http,
        poll_interval_seconds=0.01,
        poll_timeout_seconds=0.05,  # ~5 опросов максимум
        **kw,
    )


# ── 1. Happy path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_container_upload_poll_publish(tmp_path):
    http = FakeHTTP()
    store = InMemoryIdempotencyStore()
    adapter = _adapter(http, store)
    pub = new_publication_id()

    result = await adapter.publish(_reel(tmp_path), publication_id=pub)

    assert result.status is ResultStatus.DONE
    assert result.external_id == _MEDIA_ID  # квитанция = published media id
    assert http.create_calls == 1
    assert http.publish_calls == 1

    # container creation: JSON body (not form), Authorization: Bearer, video_url (not upload_type=resumable)
    # Confirmed by live API probe 2026-10: resumable not supported for Instagram Login (IGAGW) tokens.
    create_kw = http.last_create_kwargs
    assert "json" in create_kw, "container creation must use json= body, not data="
    assert "data" not in create_kw, "container creation must not use form-encoded data="
    body = create_kw["json"]
    assert body["media_type"] == "REELS"
    assert "video_url" in body, "must send video_url, not upload_type=resumable"
    assert "upload_type" not in body, "resumable upload not supported for Instagram Login tokens"
    assert "access_token" not in body, "token must go in Authorization header, not body"
    assert create_kw.get("headers", {}).get("Authorization") == f"Bearer {_TOKEN}"

    # publish: JSON body, Authorization: Bearer
    publish_kw = http.last_publish_kwargs
    assert "json" in publish_kw, "media_publish must use json= body"
    assert publish_kw["json"]["creation_id"] == _CONTAINER
    assert "access_token" not in publish_kw.get("json", {}), "token must be in header"
    assert publish_kw.get("headers", {}).get("Authorization") == f"Bearer {_TOKEN}"

    assert store.is_done(pub, "instagram", target=_IG_USER)


# ── 2. Retry after container creation reuses the container ────────────────────


@pytest.mark.asyncio
async def test_retry_after_container_reuses_it_no_second_container(tmp_path):
    """1-я попытка создаёт контейнер и упирается в таймаут опроса (остаётся IN_PROGRESS).
    2-я попытка (тот же store) НЕ создаёт второй контейнер — дожимает существующий."""
    http = FakeHTTP(status_sequence=[_resp({"status_code": "IN_PROGRESS"})])
    store = InMemoryIdempotencyStore()
    pub = new_publication_id()

    r1 = await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)
    assert r1.status is ResultStatus.FAILED
    assert http.create_calls == 1

    # теперь транскодинг завершился
    http._status_seq = [_resp({"status_code": "FINISHED"})]
    r2 = await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)

    assert r2.status is ResultStatus.DONE
    assert r2.external_id == _MEDIA_ID
    assert http.create_calls == 1  # второй контейнер НЕ создан
    assert http.publish_calls == 1
    assert store.is_done(pub, "instagram", target=_IG_USER)


# ── 3. Retry after publish already succeeded → done, no second publish ────────


@pytest.mark.asyncio
async def test_retry_after_publish_is_skipped(tmp_path):
    http = FakeHTTP()
    store = InMemoryIdempotencyStore()
    pub = new_publication_id()

    await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)
    assert http.publish_calls == 1

    r2 = await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)
    assert r2.status is ResultStatus.SKIPPED
    assert http.publish_calls == 1  # второй publish НЕ произошёл
    assert http.create_calls == 1


# ── 4. Container ERROR → target fails with API reason ─────────────────────────


@pytest.mark.asyncio
async def test_container_error_fails_with_api_reason(tmp_path):
    detail = "Error: 2207026 - The video format is not supported"
    http = FakeHTTP(status_sequence=[_resp({"status_code": "ERROR", "status": detail})])
    adapter = _adapter(http)

    result = await adapter.publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert detail in result.error
    assert http.publish_calls == 0


# ── 4a. Terminal fail → #container checkpoint deleted (Fix 1) ─────────────────


@pytest.mark.asyncio
async def test_container_error_deletes_checkpoint_so_retry_creates_fresh(tmp_path):
    """ERROR response → FAILED + #container checkpoint removed; next attempt creates a new container."""
    store = InMemoryIdempotencyStore()
    pub = new_publication_id()
    http = FakeHTTP(status_sequence=[_resp({"status_code": "ERROR", "status": "2207040"})])

    r1 = await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)
    assert r1.status is ResultStatus.FAILED

    ckpt = f"{pub}#container"
    assert store.get_external_id(ckpt, "instagram", target=_IG_USER) is None, (
        "#container checkpoint must be cleared after terminal ERROR"
    )

    # Next attempt creates a fresh container (create_calls goes to 2)
    http._status_seq = [_resp({"status_code": "FINISHED"})]
    http.create_calls = 0
    r2 = await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)
    assert r2.status is ResultStatus.DONE
    assert http.create_calls == 1, "retry must create a new container"


@pytest.mark.asyncio
async def test_container_expired_deletes_checkpoint(tmp_path):
    """EXPIRED response → FAILED + #container checkpoint cleared."""
    store = InMemoryIdempotencyStore()
    pub = new_publication_id()
    http = FakeHTTP(status_sequence=[_resp({"status_code": "EXPIRED"})])

    r1 = await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)
    assert r1.status is ResultStatus.FAILED

    ckpt = f"{pub}#container"
    assert store.get_external_id(ckpt, "instagram", target=_IG_USER) is None, (
        "#container checkpoint must be cleared after terminal EXPIRED"
    )


@pytest.mark.asyncio
async def test_polling_timeout_keeps_checkpoint_not_deleted(tmp_path):
    """httpx.ReadTimeout during polling → checkpoint survives (non-terminal)."""
    import httpx as _httpx

    store = InMemoryIdempotencyStore()
    pub = new_publication_id()

    class _TimeoutPoll(FakeHTTP):
        async def get(self, url, **kwargs):
            raise _httpx.ReadTimeout("simulated", request=None)

    r1 = await _adapter(_TimeoutPoll(), store).publish(_reel(tmp_path), publication_id=pub)
    assert r1.status is ResultStatus.FAILED

    ckpt = f"{pub}#container"
    assert store.get_external_id(ckpt, "instagram", target=_IG_USER) == _CONTAINER, (
        "#container checkpoint must survive a timeout (non-terminal failure)"
    )


@pytest.mark.asyncio
async def test_meta_error_status_subcode_appears_in_error_message(tmp_path):
    """When Meta returns a status subcode string, it appears verbatim in the error message."""
    subcode = "Error: 2207040 - The video is too long"
    http = FakeHTTP(status_sequence=[_resp({"status_code": "ERROR", "status": subcode})])

    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert subcode in result.error, (
        f"Meta status subcode must appear verbatim in error, got: {result.error!r}"
    )


# ── 5. Polling timeout → explicit failure, container kept for later retry ─────


@pytest.mark.asyncio
async def test_polling_timeout_keeps_container(tmp_path):
    http = FakeHTTP(status_sequence=[_resp({"status_code": "IN_PROGRESS"})])
    store = InMemoryIdempotencyStore()
    pub = new_publication_id()

    result = await _adapter(http, store).publish(_reel(tmp_path), publication_id=pub)

    assert result.status is ResultStatus.FAILED
    assert "таймаут" in result.error.lower() or "timeout" in result.error.lower()
    assert not store.is_done(pub, "instagram", target=_IG_USER)
    # контейнер сохранён под чекпоинтом — следующий ретрай его переиспользует
    ckpt = store.get_external_id(f"{pub}#container", "instagram", target=_IG_USER)
    assert ckpt == _CONTAINER
    assert http.publish_calls == 0


# ── 6. Expired/invalid token → needs_relogin ──────────────────────────────────


@pytest.mark.asyncio
async def test_expired_token_marks_needs_relogin(tmp_path):
    err = _resp(
        {
            "error": {
                "message": "Error validating access token: Session has expired",
                "type": "OAuthException",
                "code": 190,
            }
        },
        status_code=400,
    )
    http = FakeHTTP(create=err)
    adapter = _adapter(http)

    result = await adapter.publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.NEEDS_RELOGIN
    assert http.publish_calls == 0


# ── 7. Local validation fails early (specs) ───────────────────────────────────


def test_validate_rejects_non_video_container(tmp_path):
    bad = tmp_path / "pic.jpg"
    bad.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(Exception, match="mp4|mov|контейнер"):
        _validate_reel_file(bad)


def test_validate_rejects_oversized(tmp_path, monkeypatch):
    from dataclasses import replace

    import crosspost.channels.validators as validators_mod

    big = tmp_path / "big.mp4"
    big.write_bytes(b"\x00")
    # Override the declared limit to 0 instead of writing 300 MB to disk.
    tiny = replace(validators_mod.VALIDATORS["instagram"], max_video_bytes=0)
    monkeypatch.setitem(validators_mod.VALIDATORS, "instagram", tiny)
    with pytest.raises(Exception, match="МБ|limit|превыша"):
        _validate_reel_file(big)


def test_validate_accepts_mp4(tmp_path):
    ok = tmp_path / "clip.mov"
    ok.write_bytes(b"\x00" * 10)
    assert _validate_reel_file(ok) == 10  # returns size in bytes


@pytest.mark.asyncio
async def test_invalid_file_fails_before_any_api_call(tmp_path):
    http = FakeHTTP()
    bad = CanonicalContent(type=ContentType.REEL, text="x", media_paths=[tmp_path / "pic.jpg"])
    (tmp_path / "pic.jpg").write_bytes(b"\xff\xd8\xff")

    result = await _adapter(http).publish(bad, publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert http.create_calls == 0  # ничего не ушло на площадку


# ── regression: declarations ──────────────────────────────────────────────────


def test_instagram_declared_as_video_channel():
    from crosspost.content.canonical import ContentType as T
    from crosspost.content.capabilities import supports, supports_video

    assert supports_video("instagram") is True
    assert supports("instagram", T.REEL) is True
    # POST supported: video post → adapter publishes as Reel; VIDEO_REQUIRED_CHANNELS gates no-video
    assert supports("instagram", T.POST) is True
    # фото/сторис по-прежнему не заявлены
    assert supports("instagram", T.STORY) is False


def test_other_channels_video_support_unchanged():
    from crosspost.content.capabilities import supports_video

    assert supports_video("telegram") is True
    assert supports_video("telegraph") is False
    assert supports_video("vk_wall") is False
    assert supports_video("yandex") is False


# ── signed video_url tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_video_url_is_signed_and_verifiable(tmp_path):
    """Container creation sends video_url = base + /public-media/ + valid signed token."""
    from crosspost.web.signed_media import verify_media_token

    http = FakeHTTP()
    content = _reel(tmp_path)
    await _adapter(http).publish(content, publication_id=new_publication_id())

    body = http.last_create_kwargs["json"]
    video_url = body["video_url"]

    assert video_url.startswith(_PUBLIC_BASE + "/public-media/"), (
        f"expected signed URL, got: {video_url}"
    )

    token = video_url.removeprefix(_PUBLIC_BASE + "/public-media/")
    rel_path = verify_media_token(token)
    assert rel_path is not None, "token must be valid (not expired, not tampered)"

    # The token resolves to THIS publication's video filename
    expected_name = Path(content.media_paths[0]).name
    assert rel_path.endswith(expected_name), (
        f"token path {rel_path!r} does not point to {expected_name!r}"
    )


@pytest.mark.asyncio
async def test_unset_public_base_url_fails_before_any_api_call(tmp_path, monkeypatch):
    """INSTAGRAM_PUBLIC_BASE_URL not set → FAILED with local-address message, 0 HTTP calls."""
    monkeypatch.delenv("INSTAGRAM_PUBLIC_BASE_URL", raising=False)
    http = FakeHTTP()
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert "INSTAGRAM_PUBLIC_BASE_URL" in result.error or "локального" in result.error
    assert http.create_calls == 0


@pytest.mark.asyncio
async def test_localhost_base_url_fails_before_any_api_call(tmp_path, monkeypatch):
    """INSTAGRAM_PUBLIC_BASE_URL = http://localhost → FAILED, 0 HTTP calls."""
    monkeypatch.setenv("INSTAGRAM_PUBLIC_BASE_URL", "http://localhost:8000")
    http = FakeHTTP()
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert "локального" in result.error
    assert http.create_calls == 0


@pytest.mark.asyncio
async def test_private_ip_base_url_fails_before_any_api_call(tmp_path, monkeypatch):
    """INSTAGRAM_PUBLIC_BASE_URL = http://192.168.1.10 → FAILED, 0 HTTP calls."""
    monkeypatch.setenv("INSTAGRAM_PUBLIC_BASE_URL", "http://192.168.1.10")
    http = FakeHTTP()
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert "локального" in result.error
    assert http.create_calls == 0


@pytest.mark.asyncio
async def test_logs_contain_no_full_token(tmp_path):
    """Logger output during publish must not contain the full signed token."""
    http = FakeHTTP()
    content = _reel(tmp_path)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        await _adapter(http).publish(content, publication_id=new_publication_id())
    finally:
        root.removeHandler(capture)

    body = http.last_create_kwargs["json"]
    full_token = body["video_url"].removeprefix(_PUBLIC_BASE + "/public-media/")

    for record in records:
        msg = record.getMessage()
        assert full_token not in msg, (
            f"full token found in log record from {record.name}: {msg[:120]}"
        )


# ── timeout handling ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_httpx_timeout_during_polling_gives_russian_message_and_keeps_checkpoint(tmp_path):
    """httpx.ReadTimeout during container status polling → Russian timeout message.

    The container checkpoint must survive so a retry resumes from the checkpoint
    instead of creating a new container.
    """
    import httpx

    from crosspost.orchestrator.task import InMemoryIdempotencyStore

    store = InMemoryIdempotencyStore()

    class _TimeoutOnStatus(FakeHTTP):
        async def get(self, url, **kwargs):
            raise httpx.ReadTimeout("simulated", request=None)

    http = _TimeoutOnStatus()
    result = await _adapter(http, store=store).publish(
        _reel(tmp_path), publication_id="pub-timeout"
    )

    assert result.status is ResultStatus.FAILED
    assert result.error, "error must be non-empty"
    assert "Meta" in result.error or "таймаут" in result.error.lower(), (
        f"expected Russian timeout message, got: {result.error!r}"
    )
    assert "опрос" in result.error.lower() or "статус" in result.error.lower(), (
        f"message should mention polling step, got: {result.error!r}"
    )

    # Container checkpoint is still present — retry can resume from it.
    # The adapter stores checkpoints with target=ig_user_id (self._target).
    container_ckpt = "pub-timeout" + "#container"
    assert store.get_external_id(container_ckpt, "instagram", target=_IG_USER), (
        "container checkpoint must survive a polling timeout"
    )


# ── token header tests (Fix 1) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_poll_sends_token_in_header_not_query(tmp_path):
    """GET container status must use Authorization: Bearer, not access_token param."""
    http = FakeHTTP()
    await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())

    assert http.last_status_kwargs is not None
    params = http.last_status_kwargs.get("params", {})
    assert "access_token" not in params, "access_token must not appear in status GET query params"
    headers = http.last_status_kwargs.get("headers", {})
    assert headers.get("Authorization") == f"Bearer {_TOKEN}", (
        "status GET must carry Authorization: Bearer header"
    )


# ── _raise_for_api_error routing (Fix 2) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_oauth_code_190_gives_needs_relogin(tmp_path):
    """OAuthException code 190 → NEEDS_RELOGIN (expired token)."""
    err_resp = _resp(
        {"error": {"message": "Session has expired", "type": "OAuthException", "code": 190}},
        status_code=400,
    )
    http = FakeHTTP(create=err_resp)
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())
    assert result.status is ResultStatus.NEEDS_RELOGIN


@pytest.mark.asyncio
async def test_oauth_permission_code_10_gives_failed_with_verbatim_meta_message(tmp_path):
    """OAuthException code 10 → FAILED; error contains Meta's original message verbatim."""
    err_resp = _resp(
        {
            "error": {
                "message": "App does not have permission",
                "type": "OAuthException",
                "code": 10,
            }
        },
        status_code=403,
    )
    http = FakeHTTP(create=err_resp)
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())
    assert result.status is ResultStatus.FAILED
    assert "App does not have permission" in result.error, (
        f"Meta's verbatim message must be in error, got: {result.error!r}"
    )
    assert "instagram_business_content_publish" in result.error, (
        f"hint with permission name must appear in error, got: {result.error!r}"
    )


@pytest.mark.asyncio
async def test_oauth_code_200_api_access_blocked_gives_failed_with_verbatim_message(tmp_path):
    """OAuthException code 200 'API access blocked' → FAILED with Meta's message verbatim and hint."""
    err_resp = _resp(
        {"error": {"message": "API access blocked.", "type": "OAuthException", "code": 200}},
        status_code=400,
    )
    http = FakeHTTP(create=err_resp)
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())
    assert result.status is ResultStatus.FAILED
    assert "API access blocked." in result.error, (
        f"Meta's verbatim message must be in error, got: {result.error!r}"
    )
    assert "instagram_business_content_publish" in result.error, (
        f"hint must mention permission name, got: {result.error!r}"
    )


@pytest.mark.asyncio
async def test_other_oauth_exception_gives_failed_not_relogin(tmp_path):
    """OAuthException with other codes → FAILED (not NEEDS_RELOGIN)."""
    err_resp = _resp(
        {"error": {"message": "Some OAuth error", "type": "OAuthException", "code": 999}},
        status_code=400,
    )
    http = FakeHTTP(create=err_resp)
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())
    assert result.status is ResultStatus.FAILED
    assert result.status is not ResultStatus.NEEDS_RELOGIN


# ── proxy support ─────────────────────────────────────────────────────────────


def test_adapter_new_client_uses_proxy_when_env_set(monkeypatch):
    """_new_client() passes proxy= to httpx.AsyncClient when INSTAGRAM_PROXY_URL is set."""
    from unittest.mock import patch

    from crosspost.adapters.api.instagram import InstagramAdapter

    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "http://127.0.0.1:10809")
    with patch("crosspost.adapters.api.instagram.httpx.AsyncClient") as mock_cls:
        InstagramAdapter._new_client()
    assert mock_cls.call_args.kwargs.get("proxy") == "http://127.0.0.1:10809"


def test_adapter_new_client_no_proxy_when_env_unset(monkeypatch):
    """_new_client() does NOT pass proxy= when INSTAGRAM_PROXY_URL is absent."""
    from unittest.mock import patch

    from crosspost.adapters.api.instagram import InstagramAdapter

    monkeypatch.delenv("INSTAGRAM_PROXY_URL", raising=False)
    with patch("crosspost.adapters.api.instagram.httpx.AsyncClient") as mock_cls:
        InstagramAdapter._new_client()
    assert "proxy" not in mock_cls.call_args.kwargs


@pytest.mark.asyncio
async def test_adapter_proxy_unreachable_gives_explicit_error(tmp_path, monkeypatch):
    """ProxyError → FAILED with explicit proxy message, not a generic network error."""
    import httpx as _httpx

    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "http://127.0.0.1:10809")

    class _ProxyDown(FakeHTTP):
        async def post(self, url, **kwargs):
            raise _httpx.ProxyError("proxy unreachable")

    result = await _adapter(_ProxyDown()).publish(
        _reel(tmp_path), publication_id=new_publication_id()
    )

    assert result.status is ResultStatus.FAILED
    assert "INSTAGRAM_PROXY_URL" in result.error
    assert "прокси" in result.error.lower() or "proxy" in result.error.lower()


@pytest.mark.asyncio
async def test_adapter_proxy_credentials_not_in_error(tmp_path, monkeypatch):
    """Proxy URL credentials (user:pass) must never appear in error messages."""
    import httpx as _httpx

    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "http://user:s3cr3t@127.0.0.1:10809")

    class _ProxyDown(FakeHTTP):
        async def post(self, url, **kwargs):
            raise _httpx.ProxyError("proxy unreachable")

    result = await _adapter(_ProxyDown()).publish(
        _reel(tmp_path), publication_id=new_publication_id()
    )

    assert result.status is ResultStatus.FAILED
    assert "s3cr3t" not in result.error
    assert "user:s3cr3t" not in result.error


@pytest.mark.asyncio
async def test_adapter_code_200_without_proxy_has_region_hint(tmp_path, monkeypatch):
    """OAuthException code 200 without proxy → error includes region-block hint with INSTAGRAM_PROXY_URL."""
    monkeypatch.delenv("INSTAGRAM_PROXY_URL", raising=False)
    err_resp = _resp(
        {"error": {"message": "API access blocked.", "type": "OAuthException", "code": 200}},
        status_code=400,
    )
    http = FakeHTTP(create=err_resp)
    result = await _adapter(http).publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert "INSTAGRAM_PROXY_URL" in result.error, (
        f"region-block hint must mention INSTAGRAM_PROXY_URL, got: {result.error!r}"
    )
