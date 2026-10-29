from __future__ import annotations

from crosspost.content.canonical import ContentType as T
from crosspost.content.capabilities import channels_for, supports, video_ineligible_reason


def test_youtube_rejects_post_accepts_reel():
    assert supports("youtube", T.POST) is False
    assert supports("youtube", T.REEL) is True


def test_channels_for_article_excludes_youtube():
    chans = channels_for(T.ARTICLE)
    assert "telegraph" in chans
    assert "youtube" not in chans


# ── Единое video-правило (его зовут publish-gate, /targets и импорт плана) ─────


def test_video_rule_rejects_video_to_incapable_channel():
    # yandex не в VIDEO_CHANNELS → видео нельзя.
    assert (
        video_ineligible_reason("yandex", None, has_video=True) == "Этот канал не принимает видео"
    )


def test_video_rule_requires_video_for_instagram():
    assert (
        video_ineligible_reason("instagram", 300, has_video=False)
        == "Прикрепите видео для публикации"
    )


def test_video_rule_rejects_oversized_video():
    max_b = 10 * 1024 * 1024
    assert (
        video_ineligible_reason("instagram", max_b, has_video=True, video_bytes=max_b + 1)
        == "Видео больше лимита канала (10 МБ)"
    )


def test_video_rule_ok_cases():
    # telegram принимает и видео, и пост без видео.
    assert video_ineligible_reason("telegram", None, has_video=True) == ""
    assert video_ineligible_reason("telegram", None, has_video=False) == ""
    # instagram с видео в пределах лимита — ок.
    assert (
        video_ineligible_reason("instagram", 300 * 1024 * 1024, has_video=True, video_bytes=1) == ""
    )
