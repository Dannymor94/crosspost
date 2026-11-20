"""T-210: VerifyFailed propagation through vk_wall._publish_step2 and vk_channel._send.

Tests that:
  - VerifyFailed("no_match") from inner poll → reason "no_match" returned as tuple[None, "no_match"]
  - Generic exception from verify path → (None, None)
  - Successful verify → (external_id, None)
"""

from __future__ import annotations

import pytest

from crosspost.adapters.base import VerifyFailed

# ── _publish_step2 unit tests (vk_wall) ──────────────────────────────────────


class _FakeStoreWall:
    async def is_done(self, pub_id, channel):
        return False

    async def mark_done(self, pub_id, channel, *, external_id=None):
        pass


def _make_wall_adapter():
    from crosspost.adapters.browser.vk_wall import VKWallBrowserAdapter

    return VKWallBrowserAdapter(screen_name="testgroup", store=_FakeStoreWall(), headless=True)


@pytest.mark.asyncio
async def test_wall_publish_step2_no_match_reason():
    """_wait_for_post raises VerifyFailed("no_match") → _publish_step2 returns (None, "no_match")."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _make_wall_adapter()
    page = MagicMock()

    # Simulate a button found and clicked
    btn = AsyncMock()
    btn.count = AsyncMock(return_value=1)
    btn.first = AsyncMock()
    btn.first.click = AsyncMock()
    page.locator = MagicMock(return_value=btn)

    # _wait_for_post raises VerifyFailed
    async def _raise_verify_failed(*args, **kwargs):
        raise VerifyFailed("no_match")

    adapter._wait_for_post = _raise_verify_failed

    external_id, reason = await adapter._publish_step2(page, "test text", set())
    assert external_id is None
    assert reason == "no_match"


@pytest.mark.asyncio
async def test_wall_publish_step2_unclassified_exception_gives_none():
    """Generic exception from verify path → (None, None); NOT labelled as network."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _make_wall_adapter()
    page = MagicMock()

    btn = AsyncMock()
    btn.count = AsyncMock(return_value=1)
    btn.first = AsyncMock()
    btn.first.click = AsyncMock()
    page.locator = MagicMock(return_value=btn)

    async def _raise_generic(*args, **kwargs):
        raise RuntimeError("playwright DOM error")

    adapter._wait_for_post = _raise_generic

    external_id, reason = await adapter._publish_step2(page, "test text", set())
    assert external_id is None
    assert reason is None


@pytest.mark.asyncio
async def test_wall_publish_step2_success_no_reason():
    """Successful verify → (external_id, None)."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _make_wall_adapter()
    page = MagicMock()

    btn = AsyncMock()
    btn.count = AsyncMock(return_value=1)
    btn.first = AsyncMock()
    btn.first.click = AsyncMock()
    page.locator = MagicMock(return_value=btn)

    async def _succeed(*args, **kwargs):
        return "wall-123_456"

    adapter._wait_for_post = _succeed

    external_id, reason = await adapter._publish_step2(page, "test text", set())
    assert external_id == "wall-123_456"
    assert reason is None


# ── _send unit tests (vk_channel) ────────────────────────────────────────────


class _FakeStoreChannel:
    async def is_done(self, pub_id, channel):
        return False

    async def mark_done(self, pub_id, channel, *, external_id=None):
        pass


def _make_channel_adapter():
    from crosspost.adapters.browser.vk_channel import VKChannelBrowserAdapter

    return VKChannelBrowserAdapter(channel_id="123456", store=_FakeStoreChannel(), headless=True)


@pytest.mark.asyncio
async def test_channel_send_no_match_reason():
    """_verify_sent raises VerifyFailed("no_match") → _send returns (None, "no_match")."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _make_channel_adapter()
    page = MagicMock()

    # Provide a ready send button
    send_btn = AsyncMock()
    send_btn.first = AsyncMock()
    send_btn.first.click = AsyncMock()
    page.locator = MagicMock(return_value=send_btn)
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()

    async def _raise_verify(*args, **kwargs):
        raise VerifyFailed("no_match")

    adapter._verify_sent = _raise_verify

    external_id, reason = await adapter._send(
        page, text="channel post", known_ids=set(), has_media=False
    )
    assert external_id is None
    assert reason == "no_match"


@pytest.mark.asyncio
async def test_channel_send_unclassified_exception_gives_none():
    """Generic exception from _verify_sent → (None, None)."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _make_channel_adapter()
    page = MagicMock()

    send_btn = AsyncMock()
    send_btn.first = AsyncMock()
    send_btn.first.click = AsyncMock()
    page.locator = MagicMock(return_value=send_btn)
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()

    async def _raise_generic(*args, **kwargs):
        raise Exception("playwright closed")

    adapter._verify_sent = _raise_generic

    external_id, reason = await adapter._send(
        page, text="channel post", known_ids=set(), has_media=False
    )
    assert external_id is None
    assert reason is None


@pytest.mark.asyncio
async def test_channel_send_success_no_reason():
    """Successful _verify_sent → (external_id, None)."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _make_channel_adapter()
    page = MagicMock()

    send_btn = AsyncMock()
    send_btn.first = AsyncMock()
    send_btn.first.click = AsyncMock()
    page.locator = MagicMock(return_value=send_btn)
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()

    async def _succeed(*args, **kwargs):
        return "channel-item42"

    adapter._verify_sent = _succeed

    external_id, reason = await adapter._send(
        page, text="channel post", known_ids=set(), has_media=False
    )
    assert external_id == "channel-item42"
    assert reason is None


# ── VerifyFailed itself ──────────────────────────────────────────────────────


def test_verify_failed_carries_reason():
    exc = VerifyFailed("no_match")
    assert exc.reason == "no_match"
    assert str(exc) == "no_match"
