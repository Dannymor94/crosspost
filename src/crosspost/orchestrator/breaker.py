"""Circuit breaker на (user, channel). Эпик 3.

Бан/жёсткий лимит → канал на паузу, задачи не диспатчатся. Защита от
углубления бана ретраями.
"""
from __future__ import annotations

# TODO(эпик 3): open/half-open/closed по (user, channel), cooldown из конфига,
# интеграция с dispatcher (проверка перед диспатчем).
