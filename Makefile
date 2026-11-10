.PHONY: check install install-mvp test lint mvp smoke up run tunnel tunnel-stop

# Четыре команды DoD — локально и в CI одинаково.
check:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src
	uv run pytest -q

# Быстрая установка для MVP: только ядро + dev. Без playwright/celery/postgres.
install-mvp:
	pip install -e ".[dev]"

# Полная установка (по мере выхода за MVP).
install:
	pip install -e ".[browser,infra,web,crypto,youtube,dev]"
	playwright install chromium

test:
	pytest -q

lint:
	ruff check src tests

# MVP-0: проверить, что CLI собирается и тесты ядра зелёные.
mvp: install-mvp test
	@echo "Ядро MVP готово. Для реальной отправки заполни runtime/.env (см. docs/OPERATIONS.md §2) и запусти make smoke."

# Финиш MVP-0: реальная отправка в TG+ВК с ключами из runtime/.env.
# Зависит от install-mvp: иначе `python -m crosspost` падает с "No module named crosspost".
smoke: install-mvp
	python -m crosspost post --type post --text "smoke test" --image runtime/sample.jpg --to telegram

# Post-MVP:
up:
	docker compose up -d
run:
	uv run uvicorn crosspost.web.app:app --reload --port 8001

# Start localhost.run SSH tunnel, update INSTAGRAM_PUBLIC_BASE_URL in runtime/.env.
# Stop with: make tunnel-stop
tunnel:
	bash scripts/tunnel.sh

tunnel-stop:
	@if [ -f /tmp/crosspost-tunnel.pid ] && kill -0 "$$(cat /tmp/crosspost-tunnel.pid)" 2>/dev/null; then \
	  kill "$$(cat /tmp/crosspost-tunnel.pid)" && echo "Tunnel stopped." || true; \
	else echo "No tunnel running."; fi; \
	rm -f /tmp/crosspost-tunnel.pid
