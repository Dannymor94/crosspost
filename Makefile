.PHONY: install install-mvp test lint mvp smoke up run

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
	@echo "Ядро MVP готово. Для реальной отправки заполни runtime/.env (см. SETUP.md) и запусти make smoke."

# Финиш MVP-0: реальная отправка в TG+ВК с ключами из runtime/.env.
# Зависит от install-mvp: иначе `python -m crosspost` падает с "No module named crosspost".
smoke: install-mvp
	python -m crosspost post --type post --text "smoke test" --image runtime/sample.jpg --to telegram,vk

# Post-MVP:
up:
	docker compose up -d
run:
	uvicorn crosspost.web.app:app --reload
