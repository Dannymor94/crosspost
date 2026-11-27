"""One-shot external monitor: run every minute on an INDEPENDENT host.

Do not install as a process within Crosspost itself: a dead VPS cannot alert.
"""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

from crosspost.ops.monitor import MonitorConfig, run_monitor_once, send_telegram_alert


def main() -> int:
    url = os.environ.get("CROSSPOST_HEALTH_URL", "")
    state_path = os.environ.get("CROSSPOST_MONITOR_STATE_PATH", "")
    token = os.environ.get("CROSSPOST_MONITOR_TG_BOT_TOKEN", "")
    chat_id = os.environ.get("CROSSPOST_MONITOR_TG_CHAT_ID", "")
    if not state_path or not token or not chat_id:
        print("Crosspost monitor configuration incomplete", file=sys.stderr)
        return 2
    try:
        config = MonitorConfig(url, Path(state_path))
    except ValueError:
        print("Crosspost monitor configuration invalid", file=sys.stderr)
        return 2

    # Avoid concurrent invocations racing to send the same alert.
    lock_file = config.state_path.with_suffix(config.state_path.suffix + ".lock")
    try:
        lock_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = run_monitor_once(
                config,
                notify=lambda s: send_telegram_alert(token=token, chat_id=chat_id, state=s),
            )
    except (OSError, RuntimeError):
        # Do not include exception strings: urllib errors may contain the bot token.
        print("Crosspost monitor check or alert failed", file=sys.stderr)
        return 2
    print(f"Crosspost monitor: {state}")
    return 0 if state == "up" else 1


if __name__ == "__main__":
    raise SystemExit(main())
