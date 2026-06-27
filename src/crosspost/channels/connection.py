"""Состояние ПОДКЛЮЧЕНИЯ (user, channel). Эпик 0/6.

Релогин и бан — свойство подключения, а НЕ отдельной задачи. Хранится один раз
на (user, channel), не дублируется по задачам.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnectionState(str, Enum):
    LIVE = "live"
    NEEDS_RELOGIN = "needs_relogin"
    BANNED = "banned"


@dataclass
class ChannelConnection:
    user: str
    channel: str
    state: ConnectionState = ConnectionState.LIVE

    @property
    def dispatchable(self) -> bool:
        return self.state is ConnectionState.LIVE
