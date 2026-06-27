from __future__ import annotations

from crosspost.content.canonical import ContentType as T
from crosspost.content.capabilities import supports, channels_for


def test_youtube_rejects_post_accepts_reel():
    assert supports("youtube", T.POST) is False
    assert supports("youtube", T.REEL) is True


def test_channels_for_article_excludes_youtube():
    chans = channels_for(T.ARTICLE)
    assert "telegraph" in chans
    assert "youtube" not in chans
