# OPERATIONS.md — эксплуатация

Запуск, конфигурация, клиенты, секреты. Деплой на сервер — фаза 3 ([`ROADMAP.md`](ROADMAP.md)).

---

## 1. Локальный запуск

```bash
uv sync --all-extras                 # или: pip install -e ".[dev,browser,infra,crypto]"
playwright install chromium          # для браузерных каналов
mkdir -p runtime
cp .env.example runtime/.env         # заполнить (см. §2)
make run                             # uv run uvicorn crosspost.web.app:app --reload --port 8001
```

Эквивалент: `python -m crosspost.web` (тот же порт и `--reload`).

Без `--reload` изменения Python-кода не подхватываются.
Если роуты отдают 404 или старое поведение, перезапустите сервер.

Проверки (Definition of Done):
```bash
ruff check src tests && ruff format --check src tests && mypy src && pytest -q
```

---

## 2. Конфигурация (переменные, которые читает код)

Источник: `os.environ` побеждает `runtime/.env` для любого ключа (при старте `load_config()`
подмешивает файл в `os.environ`, не перезаписывая заданное снаружи).

### 2.1 Веб-приложение

Учётные данные каналов хранятся в vault, а не в env.

| Группа | Переменные |
|---|---|
| Секреты (обязательны, только из окружения в проде) | `SESSION_SECRET`, `VAULT_KEY`, `MEDIA_URL_SECRET` |
| База и файлы | `DB_URL` (по умолчанию `sqlite+aiosqlite:///runtime/db.sqlite3`), `MEDIA_DIR`, `SCREENSHOT_DIR`, `RUNTIME_SETTINGS_PATH`, `BROWSER_PROFILES_DIR` |
| Браузер | `BROWSER_HEADLESS` (по умолчанию `false`), `BROWSER_SESSION_COOLDOWN_SECONDS` |
| Планировщик | `SCHEDULER_ENABLED` (true), `SCHEDULER_POLL_SECONDS` (30), `SCHEDULER_PACING_SECONDS` (5), `SCHEDULER_MAX_ATTEMPTS` (3) |
| Импорт плана | `IMPORT_PACING_SECONDS`, `IMPORT_MAX_IMAGES`, `IMPORT_MAX_CONCURRENCY`, `IMPORT_MAX_BYTES`, `IMPORT_IMAGE_MAX_BYTES` |
| Медиа | `MEDIA_URL_TTL_SECONDS` (3600), `VIDEO_MAX_BYTES` |
| Telegram | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (запасные для проверки подключения, если их нет в учётных данных канала) — K-10 |
| Instagram | `INSTAGRAM_PUBLIC_BASE_URL`, `INSTAGRAM_PROXY_URL`, `INSTAGRAM_TUNNEL_AUTO`, `INSTAGRAM_GRAPH_VERSION`, `INSTAGRAM_POLL_SECONDS`, `INSTAGRAM_POLL_TIMEOUT_SECONDS` |

### 2.2 CLI (`python -m crosspost`)

CLI-публикация берёт учётные данные каналов из env/`.env`, а не из vault.

| Канал (`--to`) | Переменные |
|---|---|
| `telegram` | `TG_API_ID`, `TG_API_HASH`, `TG_TARGET_CHANNEL`, `TG_SESSION_PATH` |
| `vk` (браузер) | `VK_GROUP_ID` |
| `vk_api` | `VK_ACCESS_TOKEN`, `VK_GROUP_ID`, `VK_PHOTO_UPLOAD_ENABLED` |
| `vk_wall` | `VK_GROUP_SCREEN_NAME` или `VK_GROUP_URL` |
| `vk_channel` | `VK_CHANNEL_ID` |
| `yandex` | `YANDEX_ORG_ID` |
| все браузерные | `BROWSER_HEADLESS` |
| `user-add` | `CROSSPOST_PASSWORD` (если пароль не передан флагом) |

### 2.3 Генерация секретов

```bash
# VAULT_KEY — только Fernet-ключ (hex не подойдёт: Fernet выдаст ValueError)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# SESSION_SECRET, MEDIA_URL_SECRET
python -c "import secrets; print(secrets.token_hex(32))"
```
Смена `SESSION_SECRET` разлогинивает всех пользователей.

---

## 3. Пользователи и клиенты

Регистрации в UI нет; пользователи заводятся через CLI:

```bash
python -m crosspost user-add --login admin --role admin --password <pass>
python -m crosspost user-add --login client1 --role client --profile-id <id> --password <pass>
```

**Подключение нового клиента (текущая процедура; полный регламент — T-401):**
1. Админ создаёт профиль в UI.
2. `user-add` с ролью `client` и `--profile-id` этого профиля.
3. Подключить каналы через мастер. Браузерный вход и коды Telegram проходит сам владелец аккаунта.
4. Добавить цели (для Telegram — через «Найти чаты» или вставкой ссылки `t.me`).
5. При необходимости включить модерацию постов клиента в настройках профиля.
6. Тестовая публикация во все каналы, проверка статусов.
7. Передать клиенту адрес `/client`, логин и пароль.

Особенности каналов: [`channels/telegram.md`](channels/telegram.md),
[`channels/instagram.md`](channels/instagram.md).

---

## 4. Секреты и git

Проект хранит токены и сессии **чужих аккаунтов**. Утёкший секрет считается скомпрометированным навсегда.

- В git не попадают: `runtime/`, `.env`, `*.session`, vault, токены. Перед коммитом — `git status`.
- Коммитится только `.env.example` без значений.
- `VAULT_KEY`, `SESSION_SECRET`, `MEDIA_URL_SECRET` в проде задаются окружением или
  хранилищем секретов, а не файлом в `runtime/`.
- Секрет попал в коммит → отозвать или перевыпустить, перелогинить сессию.
  Чистить историю (`git filter-repo`) имеет смысл только до пуша; после пуша — только ротация.
- Временные токены для агентов (доступ к GitHub и т.п.): минимальные права, срок 1 день,
  отзыв сразу после использования.
- Репозиторий приватный.

---

## 5. Живой smoke

Делает владелец с реальными ключами, после изменений, которые моки не покрывают
(селекторы, SDK, площадки):

```bash
python -m crosspost post --type post --text "smoke" --image <путь>.jpg --to telegram --profile <id>
```

Для браузерных каналов и Instagram — публикация из UI на тестовые цели.

---

## 6. Бэкап и восстановление

Не реализовано — T-207. До этого: копировать базу (`DB_URL`) и хранить `VAULT_KEY`
отдельно от бэкапа. Без ключа бэкап учётных данных бесполезен.

---

## 7. Диагностика

- Консоль ошибок в админке: проблемы по всем клиентам, скриншоты сбоев браузерных каналов.
- Отображение окна браузера при публикации переключается в настройках админки
  (поверх `BROWSER_HEADLESS`).
- Telegram-диагностика: `scripts/diagnostics/`.

## 8. Миграции БД (T-201)

При старте приложения выполняется Alembic `upgrade head`. Отдельно из корня
репозитория: `uv run alembic upgrade head`; `DB_URL` берётся из окружения.
Ревизии находятся в `src/crosspost/db/migrations` и входят в Python-пакет.
Новые изменения схемы оформлять новой ревизией Alembic.

Перед первым обновлением остановить приложение и сохранить резервную копию SQLite
с актуальными данными WAL (предпочтителен SQLite backup API). База без версии должна
содержать базовые таблицы, колонки и уникальные ключи старого приложения. После
проверки она получает ревизию `85b6d4dfff8f`, затем обновляется до `head`.
Нераспознанная схема останавливает запуск. Не обходить проверку ручным `stamp head`:
это пропустит необходимые изменения. `alembic current` не меняет базу.

Ревизия `t201_unify` сохраняет данные и квитанции, добавляет отсутствующие поля,
переносит статусы `published` в `done`/`failed`. В старых строках publications с NULL
временем создания/обновления время восстанавливается из доступного значения или
текущего времени миграции. SQLite DDL и версия применяются одной транзакцией.
Offline `--sql` для перехода не поддерживается. Обратная миграция запрещена из-за
необратимого преобразования статусов; откат — восстановление резервной копии вместе
с прежней версией приложения. Переход PostgreSQL отдельно не проверялся.
