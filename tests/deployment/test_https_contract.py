"""T-302 HTTPS ingress contract; no DNS, ACME, Docker or credentials required."""

from pathlib import Path

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[2]


def test_caddy_is_https_only_origin_reverse_proxy() -> None:
    caddyfile = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    assert "https://{$CROSSPOST_DOMAIN}" in caddyfile
    assert "reverse_proxy app:8001" in caddyfile
    # Site-scoped, not a catch-all virtual host: unknown Host is not served.
    assert all("http://" not in line for line in caddyfile.splitlines() if not line.startswith("#"))
    # Caddy's automatic HTTPS performs :80 -> :443 redirect / ACME.
    for unsafe in ("file_server", "root *", "handle_path", "rewrite", "log {"):
        assert unsafe not in caddyfile
    assert all(
        "public-media" not in line for line in caddyfile.splitlines() if not line.startswith("#")
    )


def test_compose_public_ports_media_isolation_and_proxy_trust() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"80:80"' in compose
    assert '"443:443"' in compose
    assert '"127.0.0.1:${CROSSPOST_PORT:-8001}:8001"' in compose
    assert "FORWARDED_ALLOW_IPS:" in compose
    assert "CROSSPOST_PROXY_IP" in compose
    # Both addresses are reserved: app must not win the proxy IP allocation race.
    assert 'ipv4_address: "${CROSSPOST_APP_IP:-172.30.92.3}"' in compose
    assert 'ipv4_address: "${CROSSPOST_PROXY_IP:-172.30.92.2}"' in compose
    assert 'INSTAGRAM_PUBLIC_BASE_URL: "https://${CROSSPOST_DOMAIN:?' in compose
    assert 'CROSSPOST_DOMAIN: "${CROSSPOST_DOMAIN:?' in compose
    assert "caddy_data:/data" in compose
    assert "caddy_config:/config" in compose
    assert "- crosspost_runtime:/app/runtime" in compose
    assert "- crosspost_backups:/app/backups" in compose
    caddy_section = compose.split("  caddy:", 1)[1].split("\nvolumes:", 1)[0]
    assert "crosspost_runtime" not in caddy_section
    assert "crosspost_backups" not in caddy_section
    assert "MEDIA_DIR" not in caddy_section
    assert "network_mode: host" not in compose
    assert 'FORWARDED_ALLOW_IPS: "*"' not in compose


def test_no_access_log_contains_signed_route_token() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    caddyfile = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    assert '"--no-access-log"' in dockerfile
    # Site access logging remains disabled. The global runtime logger must
    # sanitize upstream 502 errors, whose structured request includes the URI.
    assert "log {" not in caddyfile
    assert "log default {" in caddyfile
    assert "format filter {" in caddyfile
    assert "request delete" in caddyfile
    assert "wrap json" in caddyfile


def test_docker_ci_validates_caddy_and_retains_t301_smoke() -> None:
    workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
    assert "deploy/**" in workflow
    assert "caddy validate" in workflow
    assert "docker compose config --quiet" in workflow
    assert "docker compose up -d --no-build" in workflow
    assert "docker compose stop app" in workflow
    assert "QA_FAKE_BEARER_DO_NOT_LOG" in workflow
    assert "QA_FAKE_HEADER_NEVER_LOG" in workflow
    assert "docker compose logs --no-color caddy" in workflow
    assert "docker compose down -v" not in workflow


def test_signed_route_still_owned_by_fastapi() -> None:
    app = (ROOT / "src/crosspost/web/app.py").read_text(encoding="utf-8")
    route = (ROOT / "src/crosspost/web/routes/public_media.py").read_text(encoding="utf-8")
    assert "app.include_router(public_media.router)" in app
    assert '"/public-media/{token}"' in route
    assert "verify_media_token" in route


@pytest.mark.asyncio
async def test_proxy_headers_are_ignored_except_from_exact_caddy_ip() -> None:
    """Spoofed client XFF cannot become the rate-limit IP via a direct request."""
    observed: list[dict] = []

    async def target(scope: dict, receive: object, send: object) -> None:
        observed.append(scope)

    middleware = ProxyHeadersMiddleware(target, trusted_hosts="172.30.92.2")
    headers = [(b"x-forwarded-for", b"203.0.113.7"), (b"x-forwarded-proto", b"https")]
    for peer in ("198.51.100.20", "172.30.92.2"):
        scope = {
            "type": "http",
            "scheme": "http",
            "client": (peer, 32323),
            "headers": headers,
        }
        await middleware(scope, None, None)  # type: ignore[arg-type]

    assert observed[0]["client"] == ("198.51.100.20", 32323)
    assert observed[0]["scheme"] == "http"
    assert observed[1]["client"][0] == "203.0.113.7"
    assert observed[1]["scheme"] == "https"
