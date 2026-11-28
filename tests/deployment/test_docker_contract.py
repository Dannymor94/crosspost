"""T-301 deployment contract checks without Docker or real credentials."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER_WORKFLOW = ROOT / ".github/workflows/docker.yml"


def test_single_app_and_loopback_only() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "  redis:" not in compose
    assert "  postgres:" not in compose
    assert "  app:" in compose
    assert '"127.0.0.1:${CROSSPOST_PORT:-8001}:8001"' in compose
    assert "--privileged" not in compose
    assert "--network=host" not in compose


def test_single_scheduler_owner_and_signal_shutdown() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    assert '"--workers", "1"' in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "    init: true" in compose
    assert "    stop_grace_period: 120s" in compose
    assert "    restart: unless-stopped" in compose


def test_image_is_secrets_free_and_volume_backed() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    ignore = (ROOT / ".dockerignore").read_text()
    for required in ("SESSION_SECRET", "VAULT_KEY", "MEDIA_URL_SECRET"):
        assert f'{required}: "${{{required}:?' in compose
        assert required not in dockerfile
    assert "uv sync --locked" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "USER 10001:10001" in dockerfile
    for persistent in ("/app/runtime", "/app/backups"):
        assert persistent in compose
        assert persistent in dockerfile
    for forbidden in (".git", ".venv", "runtime", ".env", "*.session"):
        assert forbidden in ignore.splitlines()


def test_docker_smoke_runs_when_any_build_input_changes() -> None:
    workflow = DOCKER_WORKFLOW.read_text()
    # All of these are consumed by Dockerfile COPY/RUN, directly or indirectly.
    for build_input in (
        "Dockerfile",
        ".dockerignore",
        "docker-compose.yml",
        ".github/workflows/docker.yml",
        "tests/deployment/**",
        "src/**",
        "scripts/**",
        "pyproject.toml",
        "uv.lock",
    ):
        assert f"      - {build_input}\n" in workflow


def test_docker_smoke_checks_each_persistent_volume_and_runtime_resource() -> None:
    workflow = DOCKER_WORKFLOW.read_text()
    for marker in (
        "/app/backups/.t301-backup-marker",
        "/app/runtime/media/.t301-media-marker",
        "/app/runtime/profiles/.t301-profile-marker",
        "/app/runtime/settings.json",
    ):
        # Must create before the down/up cycle AND verify after recreation.
        assert workflow.count(marker) >= 2, marker
    assert "docker compose down\n" in workflow
    assert "docker compose down -v" not in workflow
