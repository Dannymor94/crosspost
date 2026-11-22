# T-301: single-process FastAPI + SQLite deployment (no Celery/Postgres services).
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONPATH=/app/src

WORKDIR /app

# Pin the installer; all Python application dependencies come from uv.lock.
RUN python -m pip install --no-cache-dir uv==0.13.9
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra browser --extra infra --extra crypto --no-install-project

# Install Chromium together with the OS libraries required by Playwright.
# Xvfb / interactive remote login is intentionally deferred to T-303.
RUN uv run --no-sync playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY src/ ./src/
COPY scripts/ ./scripts/

# Runtime paths are the only persistent writable locations; never persist
# secrets in the image or Git. Compose mounts them as dedicated named volumes.
RUN groupadd --gid 10001 crosspost \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin crosspost \
    && mkdir -p /app/runtime /app/backups \
    && chown -R 10001:10001 /app/runtime /app/backups \
    && chmod -R a+rX /ms-playwright

USER 10001:10001
EXPOSE 8001
STOPSIGNAL SIGTERM

# One worker owns both the in-memory browser login flow and the scheduler.
# Never add --reload or >1 workers without a scheduler ownership redesign.
CMD ["/app/.venv/bin/uvicorn", "crosspost.web.app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
