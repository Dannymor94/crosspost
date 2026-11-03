from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.orchestrator.task import InMemoryIdempotencyStore, new_publication_id

# All env vars the project reads, from docs/OPERATIONS.md §2.
# The hermetic fixture deletes each one so tests never inherit values from the
# developer's shell or runtime/.env.
_PROJECT_ENV_KEYS: tuple[str, ...] = (
    # secrets
    "SESSION_SECRET",
    "VAULT_KEY",
    "MEDIA_URL_SECRET",
    # db & paths (redirected to tmp_path below, listed here for the delete pass)
    "DB_URL",
    "MEDIA_DIR",
    "SCREENSHOT_DIR",
    "RUNTIME_SETTINGS_PATH",
    "BROWSER_PROFILES_DIR",
    # browser
    "BROWSER_HEADLESS",
    "BROWSER_SESSION_COOLDOWN_SECONDS",
    # scheduler
    "SCHEDULER_ENABLED",
    "SCHEDULER_POLL_SECONDS",
    "SCHEDULER_PACING_SECONDS",
    "SCHEDULER_MAX_ATTEMPTS",
    # import plan
    "IMPORT_PACING_SECONDS",
    "IMPORT_MAX_IMAGES",
    "IMPORT_MAX_CONCURRENCY",
    "IMPORT_MAX_BYTES",
    "IMPORT_IMAGE_MAX_BYTES",
    # media
    "MEDIA_URL_TTL_SECONDS",
    "VIDEO_MAX_BYTES",
    # telegram
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TG_API_ID",
    "TG_API_HASH",
    "TG_TARGET_CHANNEL",
    "TG_SESSION_PATH",
    # instagram
    "INSTAGRAM_PUBLIC_BASE_URL",
    "INSTAGRAM_PROXY_URL",
    "INSTAGRAM_TUNNEL_AUTO",
    "INSTAGRAM_GRAPH_VERSION",
    "INSTAGRAM_POLL_SECONDS",
    "INSTAGRAM_POLL_TIMEOUT_SECONDS",
    # vk
    "VK_GROUP_ID",
    "VK_ACCESS_TOKEN",
    "VK_PHOTO_UPLOAD_ENABLED",
    "VK_GROUP_SCREEN_NAME",
    "VK_GROUP_URL",
    "VK_CHANNEL_ID",
    # yandex
    "YANDEX_ORG_ID",
    # cli
    "CROSSPOST_PASSWORD",
)


@pytest.fixture(autouse=True, scope="session")
def _runtime_guard() -> Generator[None, None, None]:
    """Verify tests never create runtime/ when it did not exist before the run."""
    runtime_existed = Path("runtime").exists()
    yield
    if not runtime_existed:
        assert not Path("runtime").exists(), (
            "Tests must not create runtime/ in a clean checkout — "
            "check _hermetic_env fixture coverage"
        )


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every test from the developer's shell and from runtime/.env.

    1. Delete all project env vars so no value leaks in from the outside.
    2. Redirect path-based vars to tmp_path so the app can start without runtime/.
    3. Keep DB_URL as in-memory SQLite.
    4. Patch DEFAULT_ENV_PATH to a nonexistent tmp path so load_config() never
       reads runtime/.env even if it happens to exist on the developer's machine.

    Test-local fixtures can override any of these with a later monkeypatch.setenv call.
    """
    import crosspost.config  # local import — config module is loaded lazily

    # Step 1: wipe all project vars
    for key in _PROJECT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    # Step 2: redirect path-based vars to tmp_path
    monkeypatch.setenv("DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCREENSHOT_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "browser_profiles"))
    monkeypatch.setenv("TG_SESSION_PATH", str(tmp_path / "tg_session"))

    # Step 3: patch DEFAULT_ENV_PATH so load_config() reads nothing from disk
    monkeypatch.setattr(crosspost.config, "DEFAULT_ENV_PATH", tmp_path / "nonexistent.env")


@pytest.fixture
def store() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def publication_id() -> str:
    return new_publication_id()


@pytest.fixture
def sample_post(tmp_path: Path) -> CanonicalContent:
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff")  # минимальный jpeg-заголовок
    return CanonicalContent(type=ContentType.POST, text="Привет", media_paths=[img])


@pytest.fixture
def fake_telethon_client() -> AsyncMock:
    """Мок Telethon: send_file с одним медиа → одно сообщение (объект с .id)."""
    client = AsyncMock()
    sent = AsyncMock()
    sent.id = 12345
    client.send_file.return_value = sent
    return client


@pytest.fixture
def fake_telethon_client_album(tmp_path: Path) -> AsyncMock:
    """Мок Telethon: send_file с несколькими медиа → список сообщений (альбом)."""
    client = AsyncMock()
    msg1, msg2 = AsyncMock(), AsyncMock()
    msg1.id = 100
    msg2.id = 101
    client.send_file.return_value = [msg1, msg2]
    return client


@pytest.fixture
def sample_album(tmp_path: Path) -> CanonicalContent:
    """Пост с двумя медиа — Telethon отправит как альбом."""
    img1 = tmp_path / "img1.jpg"
    img2 = tmp_path / "img2.jpg"
    img1.write_bytes(b"\xff\xd8\xff")
    img2.write_bytes(b"\xff\xd8\xff")
    return CanonicalContent(type=ContentType.POST, text="Альбом", media_paths=[img1, img2])
