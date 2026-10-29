#!/usr/bin/env python3
"""CLI wrapper for starting the localhost.run tunnel.

Used by scripts/tunnel.sh. Shared logic lives in crosspost.tunnel.manager.
"""

import sys
from pathlib import Path

# Allow running as `python scripts/tunnel_start.py` from repo root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from crosspost.tunnel.manager import (
    _ENV_FILE,
    _PID_FILE,
    _default_runner,
    _update_env,
)


def main() -> None:
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
            import os

            os.kill(pid, 0)
            print(f"Stopping existing tunnel (PID {pid})...")
            os.kill(pid, 9)
        except (ProcessLookupError, OSError):
            pass

    print("Starting tunnel...")
    try:
        url = _default_runner()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    _update_env(url, _ENV_FILE)

    print("\nTunnel running:")
    print(f"  URL : {url}")
    print(f"  PID : {_PID_FILE.read_text().strip()}  (saved to {_PID_FILE})")
    print(f"\nINSTAGRAM_PUBLIC_BASE_URL updated in {_ENV_FILE}")
    print("The running server picks up the new URL automatically (no restart needed).")


if __name__ == "__main__":
    main()
