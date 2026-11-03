"""Tunnel health check and auto-restart for Instagram (localhost.run).

Active only when INSTAGRAM_TUNNEL_AUTO=1 (local dev only).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

import httpx

from crosspost.config import parse_bool
from crosspost.tunnel import state

_PID_FILE = Path("/tmp/crosspost-tunnel.pid")
_TUNNEL_LOG = Path("/tmp/crosspost-tunnel.log")
_ENV_FILE = Path("runtime/.env")
_URL_PAT = re.compile(r"https://[a-z0-9]+\.lhr\.life")
_GIVE_UP_S = 30.0
_HEALTH_TIMEOUT_S = 5.0

_ERR_TUNNEL_FAILED = (
    "Не удалось поднять туннель для Instagram (localhost.run). "
    "Проверьте интернет/VPN и запустите `make tunnel` вручную."
)


class TunnelError(Exception):
    pass


async def _check_health(base_url: str, nonce: str, timeout: float = _HEALTH_TIMEOUT_S) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/tunnel-health")
            return resp.status_code == 200 and resp.json().get("nonce") == nonce
    except Exception:
        return False


def _kill_existing() -> None:
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 9)
    except Exception:
        pass
    _PID_FILE.unlink(missing_ok=True)


def _default_runner(port: int = 8000) -> str:
    """Start an SSH tunnel to localhost.run. Blocks until URL appears. Returns https URL."""
    import time

    _TUNNEL_LOG.unlink(missing_ok=True)
    # Child keeps its own fd; close the parent's handle after spawning (no fd leak).
    log_fh = open(_TUNNEL_LOG, "w")
    try:
        proc = subprocess.Popen(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ServerAliveInterval=30",
                "-R",
                f"80:localhost:{port}",
                "nokey@localhost.run",
            ],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    _PID_FILE.write_text(str(proc.pid))

    for _ in range(20):
        time.sleep(1)
        try:
            m = _URL_PAT.search(_TUNNEL_LOG.read_text())
            if m:
                return m.group(0)
        except Exception:
            pass
    proc.kill()
    raise TunnelError("tunnel did not produce a URL in 20 s")


# Replaceable by tests.
_runner = _default_runner


def _update_env(url: str, env_file: Path = _ENV_FILE) -> None:
    """Replace INSTAGRAM_PUBLIC_BASE_URL line in env_file (best-effort)."""
    try:
        if env_file.exists():
            lines = env_file.read_text().splitlines(keepends=True)
            replaced = False
            new_lines = []
            for line in lines:
                if line.startswith("INSTAGRAM_PUBLIC_BASE_URL="):
                    new_lines.append(f"INSTAGRAM_PUBLIC_BASE_URL={url}\n")
                    replaced = True
                else:
                    new_lines.append(line)
            if not replaced:
                new_lines.append(f"\nINSTAGRAM_PUBLIC_BASE_URL={url}\n")
            env_file.write_text("".join(new_lines))
        else:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(f"INSTAGRAM_PUBLIC_BASE_URL={url}\n")
    except Exception:
        pass


_ensure_lock = asyncio.Lock()


async def ensure_tunnel() -> str:
    """Ensure the Instagram tunnel is healthy; restart if not.

    No-op when INSTAGRAM_TUNNEL_AUTO is unset.
    Raises TunnelError when tunnel cannot be raised within _GIVE_UP_S seconds.
    Returns the current (possibly updated) public base URL.
    """
    if not parse_bool(os.environ.get("INSTAGRAM_TUNNEL_AUTO"), default=False):
        return state.get_public_base_url()

    nonce = state.get_nonce()

    async with _ensure_lock:
        current_url = state.get_public_base_url()

        if current_url and await _check_health(current_url, nonce):
            return current_url

        _kill_existing()

        try:
            loop = asyncio.get_event_loop()
            new_url = await asyncio.wait_for(
                loop.run_in_executor(None, _runner),
                timeout=_GIVE_UP_S,
            )
        except TunnelError:
            raise
        except Exception as exc:
            raise TunnelError(_ERR_TUNNEL_FAILED) from exc

        state.set_public_base_url(new_url)
        _update_env(new_url)

        if not await _check_health(new_url, nonce):
            raise TunnelError(_ERR_TUNNEL_FAILED)

        return new_url
