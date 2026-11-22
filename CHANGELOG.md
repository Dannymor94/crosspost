# Changelog

## 2026-11-22 — T-302: HTTPS ingress и защищённый публичный медиа URL (стековый Draft PR)

- Caddy 2.11.9 в отдельном контейнере: владелец указывает DNS-имя, автоматическое TLS/redirect HTTP→HTTPS, постоянные тома сертификатов, наружу только 80/443; FastAPI по-прежнему доступен только на loopback/в контейнерной сети.
- Instagram принимает только канонический публичный HTTPS-origin; Compose выводит URL из того же `CROSSPOST_DOMAIN`, что и Caddy. Подписанная раздача `/public-media/{token}` не менялась, статическое монтирование медиа запрещено.
- Доверие к `X-Forwarded-*` ограничено фиксированным IP Caddy, raw access-логи Uvicorn выключены; тестируется запрет подмены клиентского IP.
- Нет реальных DNS, сертификатов или публикаций; после мержа T-301 нужны перенос ветки на актуальный main, повторные CI/QA и операторский VPS-smoke.


## 2026-11-22 — T-301: контейнер Docker/VPS (на ревью, не развёрнут)

- Образ Python 3.13 устанавливает зависимости по `uv.lock`, Playwright Chromium и системные библиотеки.
- Compose запускает один процесс FastAPI/SQLite на `127.0.0.1:8001`; БД/WAL, медиа, настройки, браузерные профили и резервные копии сохраняются в томах при пересоздании контейнера. Redis/Postgres удалены из Compose.
- Секреты поступают с хоста; процесс работает без root, SIGTERM даёт до 120 секунд на остановку.
- Отдельный GitHub Actions smoke проверяет сборку, старт и сохранность томов; живые проверки VPS, браузерного входа T-303 и TLS T-302 ещё нужны.
- T-301-R2: запуск Docker smoke теперь охватывает изменения всех входов образа (`src`, `scripts`, `pyproject.toml`, `uv.lock`); smoke независимо проверяет сохранность обоих томов, медиа, браузерных профилей и runtime-настроек после пересоздания.

## 2026-11-19 — safety fixes T-203-R2

- Подтверждённые ban/auth сохраняют Connection даже для legacy credential без строки подключения; INSERT и UPDATE защищены снимком credential blob.
- Проверка Connection и учёт попытки выполняются одним условным SQL-запросом; демотированный канал не меняет статус/счётчик публикации и освобождает неиспользованную lease.
- Детерминированные регрессии покрывают immediate/scheduled/manual, CLOSED/HALF_OPEN, новые и существующие публикации, stale credentials и конкурентные DONE/SUBMITTED.

## 2026-11-18 — persistent circuit breaker, доработка T-203-R1

- Единый guard для immediate, scheduled и manual retry: подключение → breaker/lease → попытка.
- Типизированные FloodWait/HTTP 429 открывают breaker сразу; подтверждённый ban блокирует подключение без таймера.
- Атомарная HALF_OPEN lease, fencing результатов по epoch, явное отклонение устаревших конкурентных сбоев.
- Истёкшая проба помещается в карантин; SUBMITTED не закрывает breaker и не пересылается.
- Ошибки площадок сохраняются как безопасные категории; deferred-ретрай не теряет снимок публикации.
- Интеграционная проверка включает T-209 retention и T-202 credential CAS; контракт CB-01…CB-18 — `docs/T-203-R1.md`.

## 2026-11-18 — T-207: безопасные SQLite snapshots (смержен в main)

- `scripts/sqlite_backup.py`: консистентный SQLite online backup (WAL), CLI `backup/verify/restore`;
  восстановление только в новый файл, без перезаписи существующей БД.
- HMAC-манифест, SHA256, fingerprint внешнего `VAULT_KEY`, приватные права,
  ротация только собственных пар. Ключ не попадает в бэкап.
- `tests/ops/test_sqlite_backup.py`: WAL, restore, wrong key, tamper, no-clobber,
  ротация, права. Операторский drill на копии реальных данных ещё требуется.

## 2026-11-18 — диагностика медиа-хранилища (T-213; смержена в main)

- Добавлена команда `python -m crosspost.media.diagnostics`: отчёт по каталогам,
  размерам, ссылкам SQLite и кандидатам в orphan без изменения файлов и БД.
- Вывод в текстовом или JSON-формате; симлинки не обходятся, ошибка структуры
  БД/JSON приводит к остановке. Кандидаты **не подлежат автоматическому удалению**.
- Одиннадцать синтетических тестов; реальный запуск на операторской базе ожидает smoke.

## 2026-11-11 — отключено автоудаление медиа (T-209)

- Все пути удаления медиа (финализация в executor, retry, cancel, PATCH, reject, publish_now)
  перестали удалять файлы. Медиа накапливается до ручной чистки (T-214).
- Тест на каждый путь: падает при возврате вызова `cleanup_media`.

## 2026-11-18 — Health-check сессий и fail-closed dispatch (T-202)

- `sessions/health.py`: `HealthChecker` — фоновый asyncio.Task, периодически проверяет LIVE-подключения
  по всем профилям. Временные сбои сети → состояние сохраняется (`confirmed_dead=False`).
  `confirmed_dead=True` → атомарный blob-CAS (`set_needs_relogin_if_live`). BANNED никогда не перезаписывается.
- `db/profile_repo.py`: `get_credential_with_blob` — единый согласованный снимок (plaintext + raw blob).
  `set_needs_relogin_if_live` — один SQL `UPDATE WHERE EXISTS(blob=snapshot)`, без отдельного SELECT.
- `orchestrator/adapter_factory.py`: fail-closed — `needs_relogin` или `banned` → `build_profile_adapter`
  возвращает None без попытки публикации.
- `web/app.py`: `HealthChecker` запускается в lifespan; флаг `HEALTH_CHECK_ENABLED`, интервал `HEALTH_CHECK_POLL_SECONDS`.
- Внешние исключения в `run_forever`/`run_once`/`_check_profile` логируются только как `type(exc).__name__` (без трейсбека).
- Тесты (39 новых): мёртвая сессия, временный сбой, бан, fail-closed dispatch, изоляция профилей,
  VK shared session, отмена, изоляция ошибок, Telegraph 429/5xx, Instagram 5xx/token_error,
  Telegram network error, browser network error, ScheduledExecutor retry, blob-CAS (5 сценариев), outer-loop secret sanitization (2).
- Итог: 1161 passed, 0 failed.

## 2026-11-05 — контракт VK (T-208)

- После клика VK возвращает submitted без реальной квитанции, включая таймаут
  и ошибку подтверждения. Ошибки до клика остаются ошибками.
- Текст сравнивается целиком после нормализации пробелов в обоих адаптерах.
- DOM-кандидаты не повышаются до DONE; реальные квитанции — T-502 после живого smoke.
- Оба интерфейса показывают «Отправлено, подтверждение недоступно».
- Удалён тест стабилизации, который не обнаруживал её отключение.

## 2026-11-05 (T-208, первоначальное исправление; контракт уточнён выше)

- `vk_wall` и `vk_channel`: успех требует поста с совпадающим текстом и непустым ID.
  После отправки ID должен отсутствовать в снимке ленты до отправки. Старый чужой пост,
  исчезнувшая/очищенная форма, живой JavaScript, таймаут и квитанция-заглушка не дают DONE.
- Ошибки чтения ленты пробрасываются без checkpoint; повтор проверяет ленту до отправки.
  Без текста публикация отклоняется до отправки: сопоставление медиа пока не реализовано.
- Регрессии охватывают ветки кнопок, загрузки, ожиданий, исключений и чтения квитанции.
  Тест Яндекса достигает отправки и проверяет ошибку ожидания карточки; адаптер не изменён.

## 2026-11-04 (T-204)

### Российский прокси для браузерных каналов (T-204)

- `proxy_env_var`: `VK_PROXY_URL` для `vk_wall` и `vk_channel`, `YANDEX_PROXY_URL` для `yandex`.
- `base_browser._get_proxy(channel)` определяет обязательный прокси для публикации,
  проверки сессии и UI/CLI-логина. Пустая обязательная настройка вызывает явную ошибку.
- Proxy credentials декодируются и передаются Playwright отдельно; непроксируемый WebRTC/UDP
  отключён аргументом Chromium. Ошибка старта логина через прокси возвращает HTTP 400.
- Старый браузерный канал `vk` удалён; CLI предлагает `vk_wall`.

## 2026-11-04 (T-201)

### Единая система миграций Alembic

- Исправлено по ревью PR #11: batch-перестройка сохраняет `ON DELETE CASCADE` из старого inline-DDL. Схема всех таблиц после миграции сверяется с пустой БД из моделей, включая FK, ограничения и индексы; проверен откат при сбое после перестройки.
- При старте выполняется `upgrade head` через async-соединение; ручные миграции из `engine.py` удалены.
- Базы без версии проверяются, получают исходную ревизию и обновляются до `head`.
- Новая ревизия создаёт недостающие таблицы, переносит изменения колонок, индексов, уникальности целей и статусов планировщика. Данные и квитанции сохраняются.
- Миграции входят в Python-пакет. SQLite DDL и версия фиксируется одной транзакцией.
- Проверены пустая БД, исходная ревизия, замороженная схема `ede92c0`, старые ручные изменения, CLI и повторный запуск.

## 2026-11-03 (T-108)

### Обновлены ссылки на архивные доки (T-108)

- 11 заглушек: `# TODO: реализовать по PRD_BACKLOG.` → `# TODO T-NNN: ... (see docs/BACKLOG.md)`
  с правильными ID задач (T-205 для мёртвых, T-505 для будущих каналов, T-202 для сессий).
- `src/crosspost/channels/validators.py`: `PRD_BACKLOG` → `docs/BACKLOG.md`.
- `src/crosspost/__main__.py`: убрана ссылка на `MILESTONES.md`.
- `Makefile`: `SETUP.md` → `docs/OPERATIONS.md §2`.
- `grep -rnE "PRD_BACKLOG|SETUP\.md|PROJECT_GUIDE|AGENT_BRIEF|MILESTONES" src Makefile` — пусто.

## 2026-11-03 (T-107)

### Тест согласованности документации (T-107)

- `tests/test_doc_consistency.py`: два новых теста — (1) каждый ключ из `VALIDATORS`
  присутствует в таблице §1 `docs/STATUS.md`; (2) все относительные ссылки в `AGENTS.md`
  и `docs/*.md` (кроме `docs/archive/`) ведут на существующие файлы.

## 2026-11-03 (T-105)

### Команда запуска dev-сервера (T-105)

- `src/crosspost/web/__main__.py`: добавлен — `python -m crosspost.web` запускает uvicorn на порту 8001 с `--reload`.
- `Makefile`: `make run` теперь явно указывает `--port 8001`.
- `docs/OPERATIONS.md` §1: убрана устаревшая заметка про K-4 и "порт 8001 исторически"; единая команда `make run`.

## 2026-11-03 (T-106)

### Единые имена переменных Telegram (T-106)

- `src/crosspost/config.py`: `load_config()` теперь при наличии `TG_API_ID`/`TG_API_HASH` автоматически
  создаёт `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` с `DeprecationWarning`.
- `src/crosspost/__main__.py`: CLI-клиент использует каноническое `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`.
- `.env.example`: `TG_API_ID` / `TG_API_HASH` заменены на закомментированные deprecated-алиасы.
- `tests/test_config.py`: два новых теста — алиас работает, каноническое имя побеждает.
- Итог: 982 passed, 0 failed.

## 2026-11-03 (T-104)

### `.env.example` соответствует коду (T-104)

- `.env.example`: полностью переписан по реальным переменным (41 ключ); удалены устаревшие
  `APP_ENV`, `REDIS_URL`, `STATE_DB_URL`, `VAULT_DIR`, `TELEGRAPH_ACCESS_TOKEN` и др.;
  добавлены `SESSION_SECRET`, `DB_URL`, `SCHEDULER_*`, `IMPORT_*`, `INSTAGRAM_*`,
  `TELEGRAM_API_*`, `VK_*`, `YANDEX_*`, `BROWSER_SESSION_COOLDOWN_SECONDS` и другие.
- `tests/test_env_example.py`: новый тест — сканирует `src/` на обращения к `os.environ` /
  `os.getenv` / `cfg[…]` и проверяет, что множество ключей совпадает с `.env.example`;
  добавление новой переменной без записи в `.env.example` роняет тест.
- Итог: 980 passed, 0 failed.

## 2026-11-03 (T-101)

### CI зелёный и равен DoD (T-101)

- `.github/workflows/ci.yml`: переход на `astral-sh/setup-uv@v5`, матрица Python 3.11 + 3.13,
  установка `.[dev,browser,infra,crypto]`, `playwright install chromium --with-deps`.
- `Makefile`: новая цель `check` — ровно четыре команды DoD (`ruff check`, `ruff format --check`,
  `mypy src`, `pytest -q`) через `uv run`; CI вызывает `make check`.
- `.python-version`: добавлен со значением `3.13`.
- Итог: CI зелёный на обеих версиях Python; `make check` локально = CI.

## 2026-11-03 (T-103)

### Герметичные тесты (T-103)

- `tests/conftest.py`: autouse-фикстура `_hermetic_env` удаляет все проектные переменные
  окружения, перенаправляет пути (`MEDIA_DIR`, `SCREENSHOT_DIR`, `RUNTIME_SETTINGS_PATH`,
  `BROWSER_PROFILES_DIR`, `TG_SESSION_PATH`) в `tmp_path`, `DB_URL` → `:memory:`, патчит
  `DEFAULT_ENV_PATH` на несуществующий tmp-путь — `load_config()` никогда не читает `runtime/.env`.
- `tests/conftest.py`: сессионный guard `_runtime_guard` — если `runtime/` не было до прогона,
  после прогона его быть не должно.
- `src/crosspost/config.py`: `load_config(env_path=None)` — `DEFAULT_ENV_PATH` резолвится
  в момент вызова, что позволяет тестам подменять его через `monkeypatch.setattr`.
- Итог: `env -i HOME=… PATH=… uv run pytest -q` → 979 passed, 0 failed.

## 2026-11-03 (T-102)

### `load_config()` читает все переменные окружения (T-102)

- Удалён белый список `_KNOWN_KEYS`; `load_config()` теперь делает `cfg.update(os.environ)` —
  любой ключ, заданный только через env (`BROWSER_HEADLESS`, `SCHEDULER_*`, CLI-ключи ВК/Яндекса),
  виден в возвращаемом словаре без `runtime/.env`.
- Добавлен тест `test_env_only_key_visible_in_load_config` в `tests/test_config.py`.
- Итог в чистом клоне: 976 passed, 3 failed (K-3, ждёт T-103).

## 2026-11-03

### Документация (T-100)

- Документация перестроена под продакшен и работу двух агентов.
- `AGENTS.md` — единый источник правил; `CLAUDE.md` подключает его и описывает роль исполнителя.
- Новые `docs/STATUS.md`, `ROADMAP.md`, `BACKLOG.md`, `WORKFLOW.md`, `ARCHITECTURE.md`, `OPERATIONS.md`.
- Старые документы этапа MVP перенесены в `docs/archive/` без правок.
- Шаблон PR `.github/pull_request_template.md`.
- Зафиксированы известные проблемы K-1…K-12 и задачи по фазам 1–5.

## 2026-10-31

### Telegram forum topics

- `channel_targets` now stores `topic_id`, `topic_title`, `peer_access_hash`; idempotent migration in `db/engine.py`.
- Publishing to a forum topic: `reply_to=topic_id` (plain int) passed to `send_message`/`send_file`.
- General topic (`topic_id=1`) normalised to `NULL` — no `reply_to` needed.
- `parse_telegram_link()` in `content/telegram_text.py` resolves `t.me` URLs to `(target_value, raw_topic_id)`.

### Telegram discovery

- New `channels/telegram_discovery.py`: `list_postable_dialogs`, `list_forum_topics`.
- New routes in `web/routes/telegram_discovery.py`: dialogs list, topic list, bulk target create, from-link target create. Router added to `create_app()` (was missing — caused silent 404s).
- `topic_title` stored at creation time; listing never calls Telethon.
- `POST .../targets/from-link`: paste a `t.me` URL, resolves entity + topic, creates target idempotently.

### UI

- Telegram ChannelCard: "Найти чаты" button opens picker panel (moved next to "Цели:" label, was buried in card footer).
- Paste input in TargetList for Telegram: paste a `t.me` link → auto-create target.
- TargetRow shows "Ветка: \<topic title\>" when `topic_id` is set.
- Platform grouping in PublishView and ImportView: channels grouped by `platform` / `platform_title` from validator declarations.

### Tests & tooling

- `tests/web/test_app_routes_smoke.py`: builds real app via `create_app()`, asserts all 44 routes used by `index.html` are registered. Catches missing router includes.
- `tests/web/test_index_html_syntax.py`: esbuild syntax check of the module script in `index.html`.
- `scripts/diagnostics/tg_topic_probe.py`: live probe for Telegram forum topic publishing (admin use).
- `scripts/diagnostics/backfill_topic_title.py`: one-off CLI to set `topic_title` on existing targets.

### Fixes

- `scheduled_posts.reject_reason` column migration added (was missing, caused startup crash on existing DBs).
- `telegram_discovery.router` included in `create_app()` (was missing).
