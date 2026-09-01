"""Browser-tier per-account session cooldown.

VK and other browser-tier platforms issue anti-bot challenges when the same
account opens multiple automated browser sessions in a short window. This
module gates dispatch: a second session on the same (profile_id, channel)
within the cooldown window is deferred without consuming a retry attempt.

The last-session timestamp survives restarts via profile_settings
(key `browser_session:{channel}`), so a restart after a failure does not
reset the cooldown window — the worst-case scenario.

Fan-out: cooldown is keyed on (profile_id, channel), not on target. Two
targets on the same channel share one account, so the second target is
gated behind the first within the same publication tick.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.settings_repo import ProfileSettingsRepository

# Browser-tier channels: session-based, subject to per-account anti-bot limits.
# API-tier channels (telegram, telegraph, vk, youtube) are intentionally excluded.
BROWSER_CHANNELS: frozenset[str] = frozenset(
    {"yandex", "vk_wall", "vk_channel", "vk_clips", "whatsapp", "instagram", "dzen"}
)

_KEY_PREFIX = "browser_session"


def account_group(channel: str) -> str:
    """Return the cooldown bucket for a channel.

    Channels sharing one account (e.g. all VK channels) declare the same
    account_group in their ChannelValidatorDef. That group name is used as the
    cooldown key so that a session opened by any one of them gates all others.

    Falls back to the channel name itself for channels without a declared group
    (each channel is its own bucket).
    """
    # Import here to avoid a circular import at module load time
    # (validators imports capabilities; capabilities is imported early).
    from crosspost.channels.validators import VALIDATORS  # noqa: PLC0415

    v = VALIDATORS.get(channel)
    return (v.account_group or channel) if v is not None else channel


def _key(group: str) -> str:
    return f"{_KEY_PREFIX}:{group}"


class BrowserCooldown:
    """Per-(profile_id, channel) cooldown backed by profile_settings."""

    def __init__(self, session: AsyncSession, *, cooldown_seconds: float) -> None:
        self._s = session
        self._cd = cooldown_seconds

    def is_browser_channel(self, channel: str) -> bool:
        return channel in BROWSER_CHANNELS

    async def is_cooling_down(self, profile_id: int, channel: str, now: datetime) -> bool:
        """True if this account is still within the cooldown window.

        Keyed on (profile_id, account_group) so that all channels sharing one
        VK account gate each other, not just same-channel fan-out.
        """
        if not self.is_browser_channel(channel) or self._cd <= 0:
            return False
        grp = account_group(channel)
        repo = ProfileSettingsRepository(self._s, profile_id=profile_id)
        stored = await repo.get(_key(grp))
        if stored is None:
            return False
        try:
            last_str = stored["last"]
            last = datetime.fromisoformat(last_str).replace(tzinfo=None)
        except (KeyError, ValueError):
            return False
        now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
        return (now_naive - last).total_seconds() < self._cd

    async def record_session(self, profile_id: int, channel: str, now: datetime) -> None:
        """Record that a browser session was opened for this account now.

        Call this BEFORE the adapter, so that even if the adapter crashes,
        the cooldown window is honoured on the next retry.
        """
        grp = account_group(channel)
        repo = ProfileSettingsRepository(self._s, profile_id=profile_id)
        now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
        await repo.set(_key(grp), {"last": now_naive.isoformat()})
