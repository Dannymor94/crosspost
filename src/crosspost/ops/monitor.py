"""External one-shot availability monitor with durable transition deduplication.

Run from an independent host/uptime service. A dead Crosspost process or VPS
cannot self-report its own downtime.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_STATES = frozenset({"up", "degraded", "down"})


@dataclass(frozen=True)
class MonitorConfig:
    health_url: str
    state_path: Path
    timeout_seconds: float = 5

    def __post_init__(self) -> None:
        url = urllib.parse.urlsplit(self.health_url)
        local_http = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            (url.scheme != "https" and not local_http)
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or self.timeout_seconds <= 0
            or not math.isfinite(self.timeout_seconds)
        ):
            raise ValueError("Monitor needs an HTTPS URL (or local loopback), without credentials")


def probe_health(url: str, *, timeout_seconds: float = 5) -> str:
    """Require the readiness JSON contract, not just an HTTP 200."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return "down"
            payload = json.loads(response.read(4096))
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        return "down"
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or type(payload.get("error_spike")) is not bool
    ):
        return "down"
    return "degraded" if payload.get("error_spike") is True else "up"


def send_telegram_alert(*, token: str, chat_id: str, state: str) -> None:
    """Send a bounded-retry, non-secret Telegram alert; never log transport errors."""
    if not token or not chat_id or state not in _STATES:
        raise ValueError("Monitor Telegram credentials or state invalid")
    text = {
        "down": "Crosspost: сервис недоступен или планировщик остановлен.",
        "degraded": "Crosspost: превышен порог ошибок публикаций.",
        "up": "Crosspost: работоспособность восстановлена, ошибок выше порога нет.",
    }[state]
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            request = urllib.request.Request(endpoint, data=body, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(4096))
                if (
                    response.status == 200
                    and isinstance(payload, dict)
                    and payload.get("ok") is True
                ):
                    return
        except (
            OSError,
            ValueError,
            TimeoutError,
            urllib.error.URLError,
            http.client.HTTPException,
        ):
            pass  # secrets may appear in HTTP exceptions; suppress all details
        if attempt < 2:
            time.sleep(1)
    raise RuntimeError("Telegram monitoring alert could not be delivered")


def _read_state(path: Path) -> str | None:
    try:
        state = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    return state if state in _STATES else None


def _write_state(path: Path, state: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", prefix=".monitor-", dir=path.parent, delete=False
        ) as tmp:
            name = tmp.name
            os.fchmod(tmp.fileno(), 0o600)
            tmp.write(state)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def run_monitor_once(
    config: MonitorConfig,
    *,
    probe: Callable[[str], str] | None = None,
    notify: Callable[[str], None],
) -> str:
    """Deliver only transitions; failed notifications leave old state for retry."""
    current = (probe or (lambda url: probe_health(url, timeout_seconds=config.timeout_seconds)))(
        config.health_url
    )
    if current not in _STATES:
        current = "down"
    previous = _read_state(config.state_path)
    if current != previous:
        if previous is not None or current != "up":
            notify(current)
        _write_state(config.state_path, current)
    return current
