# OPERATIONS.md — эксплуатация

Запуск, конфигурация, клиенты, секреты. Деплой на сервер — фаза 3 ([`ROADMAP.md`](ROADMAP.md)).

---

## 1. Локальный запуск

```bash
uv sync --all-extras                 # или: pip install -e ".[dev,browser,infra,crypto]"
playwright install chromium          # для браузерных каналов
cp .env.example runtime/.env         # заполнить (см. §2); .env.example устарел — K-4
make run                             # uvicorn crosspost.web.app:app --reload
```

Порт по умолчанию у uvicorn — 8000; исторически использовался 8001
(`uvicorn crosspost.web.app:app --reload --port 8001`). Единая команда — T-105.

Без `--reload` изменения Python-кода не подхватываются.
Если роуты отдают 404 или старое поведение, перезапустите сервер.

Проверки (Definition of Done):
```bash
ruff check src tests && ruff format --check src tests && mypy src && pytest -q
```

---

## 2. Конфигурация (переменные, которые читает код)

Источник: `os.environ`, затем `runtime/.env`. **Внимание (K-2):** до исправления T-102
часть переменных из окружения игнорируется, если их нет в `runtime/.env`.

| Группа | Переменные |
|---|---|
| Секреты (обязательны, только из окружения в проде) | `SESSION_SECRET`, `VAULT_KEY`, `MEDIA_URL_SECRET` |
| База и файлы | `DB_URL` (по умолчанию `sqlite+aiosqlite:///runtime/db.sqlite3`), `MEDIA_DIR`, `SCREENSHOT_DIR`, `RUNTIME_SETTINGS_PATH`, `BROWSER_PROFILES_DIR` |
| Браузер | `BROWSER_HEADLESS` (по умолчанию `false`), `BROWSER_SESSION_COOLDOWN_SECONDS` |
| Планировщик | `SCHEDULER_ENABLED` (true), `SCHEDULER_POLL_SECONDS` (30), `SCHEDULER_PACING_SECONDS` (5), `SCHEDULER_MAX_ATTEMPTS` (3) |
| Импорт плана | `IMPORT_PACING_SECONDS`, `IMPORT_MAX_IMAGES`, `IMPORT_MAX_CONCURRENCY`, `IMPORT_MAX_BYTES`, `IMPORT_IMAGE_MAX_BYTES` |
| Медиа | `MEDIA_URL_TTL_SECONDS` (3600), `VIDEO_MAX_BYTES` |
| Telegram | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (запасные, если не заданы в учётных данных канала), `TG_SESSION_PATH`; в `.env` также встречается `TG_API_ID` — K-10 |
| Instagram | `INSTAGRAM_PUBLIC_BASE_URL`, `INSTAGRAM_PROXY_URL`, `INSTAGRAM_TUNNEL_AUTO`, `INSTAGRAM_GRAPH_VERSION`, `INSTAGRAM_POLL_SECONDS`, `INSTAGRAM_POLL_TIMEOUT_SECONDS` |
| ВК | `VK_GROUP_URL`, `VK_GROUP_SCREEN_NAME`, `VK_PHOTO_UPLOAD_ENABLED` |
| CLI | `CROSSPOST_PASSWORD` (пароль для `user-add`, если не передан флагом) |

Сгенерировать секрет: `python -c "import secrets; print(secrets.token_hex(32))"`.
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
