"""T-208: UI transitions cannot acknowledge an undelivered publication."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.base import ResultStatus
from crosspost.adapters.browser import vk_channel, vk_wall
from crosspost.content.canonical import CanonicalContent, ContentType

TEXT = "the publication being sent"


def _post(key, text):
    return AsyncMock(
        inner_text=AsyncMock(return_value=text),
        get_attribute=AsyncMock(return_value=key),
    )


def _page(module, *, before=(), after=(), effect=None, fallback=0):
    """Model delivery separately from composer/modal state and button clicks."""
    page = AsyncMock()
    sent = False

    async def click(*args, **kwargs):
        nonlocal sent
        sent = True

    async def posts():
        if not sent and effect == "snapshot_error":
            raise OSError("cannot establish baseline")
        if sent and effect in {"list_error", "read_error", "id_error"}:
            if effect == "list_error":
                raise OSError("connection lost")
            post = _post("new", TEXT)
            getattr(
                post, "inner_text" if effect == "read_error" else "get_attribute"
            ).side_effect = OSError("connection lost")
            return [post]
        return list(after if sent else before)

    def locator(selector, **kwargs):
        loc = AsyncMock()
        loc.first = AsyncMock()
        loc.first.count.return_value = 1
        loc.count.return_value = 1
        loc.first.inner_text.return_value = "" if effect != "composer_stuck" else TEXT
        if effect == "composer_detached":
            loc.first.inner_text.side_effect = RuntimeError("element detached")
        loc.first.get_attribute.return_value = "old"
        loc.nth = MagicMock(return_value=loc.first)
        loc.all.side_effect = posts
        if selector.startswith("[class*='Composer']"):
            loc.all = AsyncMock(return_value=[])
            if effect == "debug_list_error":
                loc.all.side_effect = OSError("debug read failed")
            elif effect == "debug_item_error":
                loc.all.return_value = [
                    AsyncMock(evaluate=AsyncMock(side_effect=OSError("detached")))
                ]
        if "file" in selector and effect in {"file_fallback", "attach_text_error", "no_attach"}:
            loc.first.count.return_value = 0
        if effect == "no_attach" and selector == vk_channel._ATTACH_BUTTON_SELECTOR:
            loc.first.count.return_value = 0
        if selector in (
            '[data-testid="posting_confirm_screen_submit"]',
            '[data-testid="posting_footer_button_send"]',
        ):
            index = 0 if "confirm_screen" in selector else 1
            loc.count.return_value = int(index == fallback)
            loc.first.click.side_effect = click
        if selector == vk_channel._SEND_BUTTON_SELECTOR:
            loc.first.click.side_effect = (
                OSError("click failed") if effect == "send_click_error" else click
            )
        return loc

    page.locator = MagicMock(side_effect=locator)

    def get_by_role(role, **kwargs):
        button = AsyncMock()
        if kwargs.get("name") == "Опубликовать":
            button.click.side_effect = click
        return button

    page.get_by_role = MagicMock(side_effect=get_by_role)
    page.get_by_text = MagicMock(return_value=MagicMock(first=AsyncMock()))
    if effect == "attach_text_error":
        page.get_by_text.return_value.first.click.side_effect = OSError("photo menu disappeared")
    if effect == "button_timeout":
        page.wait_for_function.side_effect = TimeoutError("button disabled")
    if effect == "photo_count_timeout":
        page.wait_for_function.side_effect = [TimeoutError("preview count"), None]

    page.evaluate.return_value = 1

    async def wait_selector(selector, **kwargs):
        if effect == "modal_timeout" and kwargs.get("state") == "detached":
            raise TimeoutError("modal still open")
        if effect == "menu_timeout" and selector == vk_wall._CREATE_MENU_SELECTOR:
            raise TimeoutError("menu")
        if (
            effect in {"focus_timeout", "debug_list_error", "debug_item_error"}
            and selector == vk_channel._COMPOSER_INPUT_SELECTOR
        ):
            raise TimeoutError("composer")
        if effect == "preview_timeout" and "img" in selector:
            raise TimeoutError("preview")

    page.wait_for_selector.side_effect = wait_selector
    return page


@asynccontextmanager
async def _open(page):
    yield page


_COMMON_FAILURES = [
    "empty",
    "old_other",
    "old_same_id",
    "missing_id",
    "wrong_text",
    "list_error",
    "read_error",
    "id_error",
    "snapshot_error",
]
_WALL_FAILURES = _COMMON_FAILURES + [
    "modal_timeout",
    "menu_timeout",
    "preview_timeout",
    "photo_count_timeout",
    "file_fallback",
]
_CHANNEL_FAILURES = _COMMON_FAILURES + [
    "composer_detached",
    "composer_stuck",
    "focus_timeout",
    "preview_timeout",
    "file_fallback",
    "attach_text_error",
    "no_attach",
    "button_timeout",
    "send_click_error",
    "debug_list_error",
    "debug_item_error",
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,scenario,fallback",
    [(vk_wall, scenario, fallback) for scenario in _WALL_FAILURES for fallback in (0, 1, 2)]
    + [(vk_channel, scenario, 0) for scenario in _CHANNEL_FAILURES],
    ids=[
        f"wall-{scenario}-button{fallback}" for scenario in _WALL_FAILURES for fallback in (0, 1, 2)
    ]
    + [f"channel-{scenario}" for scenario in _CHANNEL_FAILURES],
)
async def test_undelivered_post_never_gets_checkpoint(
    store, publication_id, monkeypatch, module, scenario, tmp_path, fallback
):
    monkeypatch.setattr(vk_channel, "_VERIFY_POLLS", 2)
    monkeypatch.setattr(vk_wall, "_POST_APPEAR_TIMEOUT", 1)
    before = [_post("old", "another publication")]
    after = before
    if scenario == "empty":
        before = after = []
    elif scenario == "old_same_id":
        after = [_post("old", TEXT)]  # existing item changed, no new receipt
    elif scenario == "missing_id":
        after = [_post("", TEXT)]
    elif scenario == "wrong_text":
        after = [_post("new", "another publication")]
    page = _page(module, before=before, after=after, effect=scenario, fallback=fallback)
    content = CanonicalContent(type=ContentType.POST, text=TEXT, media_paths=[])
    if scenario in {
        "preview_timeout",
        "file_fallback",
        "attach_text_error",
        "no_attach",
        "photo_count_timeout",
    }:
        content.media_paths = [tmp_path / "photo.jpg"]
    adapter = (
        vk_wall.VKWallBrowserAdapter("group", store)
        if module is vk_wall
        else vk_channel.VKChannelBrowserAdapter("-1", store)
    )
    with (
        patch.object(module, "open_page", lambda *a, **kw: _open(page)),
        patch.object(module, "is_logged_in", AsyncMock(return_value=True)),
    ):
        with pytest.raises((RuntimeError, OSError, TimeoutError)):
            await adapter.publish(content, publication_id=publication_id)
    assert not await store.is_done(publication_id, adapter.channel)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,fallback", [(vk_wall, 0), (vk_wall, 1), (vk_wall, 2), (vk_channel, 0)]
)
async def test_new_matching_receipt_confirms_delivery(
    store, publication_id, monkeypatch, module, fallback
):
    monkeypatch.setattr(vk_channel, "_VERIFY_POLLS", 2)
    old = _post("old", "another publication")
    page = _page(module, before=[old], after=[old, _post("new", TEXT)], fallback=fallback)
    adapter = (
        vk_wall.VKWallBrowserAdapter("group", store)
        if module is vk_wall
        else vk_channel.VKChannelBrowserAdapter("-1", store)
    )
    content = CanonicalContent(type=ContentType.POST, text=TEXT, media_paths=[])
    with (
        patch.object(module, "open_page", lambda *a, **kw: _open(page)),
        patch.object(module, "is_logged_in", AsyncMock(return_value=True)),
    ):
        result = await adapter.publish(content, publication_id=publication_id)
    assert result.status is ResultStatus.DONE
    assert result.external_id == ("wallnew" if module is vk_wall else "channel-itemnew")
    assert await store.is_done(publication_id, adapter.channel)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [vk_wall, vk_channel], ids=["wall", "channel"])
async def test_retry_recovers_only_matching_post_with_receipt(store, publication_id, module):
    page = _page(module, before=[_post("existing", TEXT)])
    adapter = (
        vk_wall.VKWallBrowserAdapter("group", store)
        if module is vk_wall
        else vk_channel.VKChannelBrowserAdapter("-1", store)
    )
    content = CanonicalContent(type=ContentType.POST, text=TEXT, media_paths=[])
    with (
        patch.object(module, "open_page", lambda *a, **kw: _open(page)),
        patch.object(module, "is_logged_in", AsyncMock(return_value=True)),
    ):
        result = await adapter.publish(content, publication_id=publication_id)
    assert result.status is ResultStatus.DONE
    assert result.external_id.endswith("existing")
    assert await store.is_done(publication_id, adapter.channel)
    page.get_by_role.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [vk_wall, vk_channel], ids=["wall", "channel"])
async def test_media_without_searchable_text_cannot_be_acknowledged(store, publication_id, module):
    page = _page(module, before=[_post("old", "another post")])
    adapter = (
        vk_wall.VKWallBrowserAdapter("group", store)
        if module is vk_wall
        else vk_channel.VKChannelBrowserAdapter("-1", store)
    )
    content = CanonicalContent(type=ContentType.POST, text="  ", media_paths=[])
    with (
        patch.object(module, "open_page", lambda *a, **kw: _open(page)),
        patch.object(module, "is_logged_in", AsyncMock(return_value=True)),
    ):
        with pytest.raises(RuntimeError, match="без текста"):
            await adapter.publish(content, publication_id=publication_id)
    assert not await store.is_done(publication_id, adapter.channel)
