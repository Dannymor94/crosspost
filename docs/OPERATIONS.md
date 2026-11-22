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

## 6. Бэкап и восстановление (T-207 — код в review, живой drill ожидается)

**Границы:** скрипт `scripts/sqlite_backup.py` создаёт консистентный *online* snapshot SQLite через
`sqlite3.Connection.backup()` (без копирования сырого DB-файла при активном WAL).
Архив включает **только SQLite БД**; `MEDIA_DIR`, `runtime/settings.json`, вложения и ключ
`VAULT_KEY` в него **не** входят. Для полного восстановления медиа и настроек нужны
отдельные защищённые резервные копии. База содержит клиентские данные в открытом виде:
права `0700`/`0600` защищают только локально, для хранения вне сервера нужно шифрование
внешним хранилищем и независимая копия.

`VAULT_KEY` должен быть доступен процессу через **защищённое окружение/secret manager**
(не передавать значение параметром CLI и не записывать в историю shell). Ключ хранится
отдельно в защищённом хранилище с проверенной процедурой извлечения; манифест содержит
только SHA256 fingerprint и HMAC, а **не** сам ключ. Без исходного ключа восстановленные
учётные данные каналов не расшифруются.

Создать архив (путь к SQLite-файлу извлечь из фактического `DB_URL`):
```bash
python scripts/sqlite_backup.py backup --db runtime/db.sqlite3 --dest /srv/private/crosspost-backups --keep 7
```
После `BACKUP_OK` появляются `crosspost-<time>-<id>.sqlite3` и `<same>.json`.
Ротация удаляет только собственные полные архивные пары **для того же ключа**, оставляя
последние семь; `--keep 0` запрещает автоматическое удаление. Не хранить каталог
бэкапов под публичным web-root, в Git или в открытой папке.

Проверить архив (ключ должен совпадать):
```bash
python scripts/sqlite_backup.py verify --snapshot /srv/private/crosspost-backups/crosspost-YYYYMMDDTHHMMSSZ-XXXXXXXXXXXX.sqlite3
```
Проверяются SHA256 файла, HMAC манифеста, отпечаток ключа, `PRAGMA integrity_check`
и обязательные таблицы Crosspost. HMAC защищает целостность, **не шифрует** сам DB-файл.

**Восстановление (только с остановленным сервисом для последующего переключения):**
1. На *копии/тестовом контуре* извлечь нужный `VAULT_KEY`, выполнить `verify`.
2. Создать **новый** файл, не затирая действующий `runtime/db.sqlite3`:
   ```bash
   python scripts/sqlite_backup.py restore --snapshot /srv/private/crosspost-backups/crosspost-YYYYMMDDTHHMMSSZ-XXXXXXXXXXXX.sqlite3 --target runtime/recovered/db.sqlite3
   ```
3. Проверить восстановленные профили/подключения и ключ на тестовом контуре.
4. Остановить Crosspost и все воркеры, переключить `DB_URL` на новый файл, сохранить
   оригинальную БД для отката. Затем поднять приложение с **тем же** `VAULT_KEY`.
   Не переносить WAL/SHM из старой БД к новой.
5. Проверить здоровье приложения, профили, квитанции и наличие медиа. При ошибке
   остановить сервис и вернуть исходную БД/конфигурацию.

Скрипт никогда не перезаписывает существующий путь при restore (отказ с exit code 2).
Production-ready требует независимый QA, CI и успешное операторское восстановление
**копии реальной БД**, плюс проверку внешней шифрованной копии и сохранности ключа.


---

## 7. Диагностика

- Консоль ошибок в админке: проблемы по всем клиентам, скриншоты сбоев браузерных каналов.
- Отображение окна браузера при публикации переключается в настройках админки
  (поверх `BROWSER_HEADLESS`).
- Telegram-диагностика: `scripts/diagnostics/`.

### Аудит медиа-хранилища (T-213)

Команда только для оператора, с сервера и из корня репозитория (не предоставлять клиентам):

```bash
uv run python -m crosspost.media.diagnostics --top 20
uv run python -m crosspost.media.diagnostics --json
```

Используются `DB_URL` и `MEDIA_DIR` из окружения (по умолчанию база
`runtime/db.sqlite3`, медиа `runtime/media`). Допускаются `--db-url` и `--media-dir`.
Поддерживается текущий SQLite, база открывается в `mode=ro` без миграций. Команда
**ничего не удаляет и не изменяет**. На отсутствующей БД, повреждённых JSON-ссылках
или неподдерживаемом DB_URL выполнение прекращается с ошибкой.

Отчёт содержит общий логический размер файлов, top крупных каталогов, ссылки
`scheduled_posts.media_paths` и ключи немедленных `publications.publication_id`,
кандидаты в orphan и несопоставленные пути. Ссылки из hard-DELETE удалённых строк
восстановить невозможно; также не учитываются ещё не завершившиеся загрузки.
Поэтому **orphan — не доказательство безопасности удаления**. Для T-214 нужен
отдельный алгоритм проверки ссылок, TTL и состояния публикаций. Симлинки не
разыменовываются; JSON-отчёт может содержать имена клиентских файлов — хранить его
только в защищённом окружении, не прикладывать к публичным Issue/PR.


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

---

## 9. Docker/VPS: T-301 (код на ревью, deployment не выполнялся)

`Dockerfile` использует Python 3.13 и Playwright Chromium; `docker-compose.yml`
запускает **ровно один** процесс FastAPI/SQLite, без Redis и Postgres. Планировщик,
проверка сессий и интерактивный вход держат состояние процесса — дополнительные
воркеры, горизонтальный scale и параллельные контейнеры запрещены до отдельной
архитектуры единоличного владения scheduler и browser-login. Публикации на VPS
ещё не проверены.

**Перед стартом:** настроить на сервере `SESSION_SECRET`, `VAULT_KEY`,
`MEDIA_URL_SECRET` как **внешние переменные окружения** или через защищённый
secret-manager (не вписывать реальные значения в Compose, Dockerfile, build args,
`.env` репозитория или логи). Использовать **тот же** `VAULT_KEY` при переносе
существующей БД. Дополнительные параметры: `CROSSPOST_PORT` (по умолчанию 8001),
`BROWSER_HEADLESS=true`, `SCHEDULER_ENABLED=true`, `HEALTH_CHECK_ENABLED=true`,
`VK_PROXY_URL`, `YANDEX_PROXY_URL`, `INSTAGRAM_PROXY_URL`,
`INSTAGRAM_PUBLIC_BASE_URL`. Compose не монтирует отдельный `.env` в контейнер,
но `crosspost.config.load_config()` **прочитает** `runtime/.env`, если такой файл
уже находится в постоянном томе (например, после переноса старого окружения).
Явные переменные процесса имеют приоритет над значениями файла. Для production
хранить секреты только во внешнем защищённом окружении, `runtime/.env` не создавать.

```bash
docker compose config --quiet       # fail closed, если нет обязательных секретов
docker compose up -d --build        # создаст app + именованные тома
docker compose ps
curl --fail http://127.0.0.1:8001/ # только локальная проверка HTTP (не DB/runner health)
docker compose logs --tail=100 app
```

Снаружи порт **не открыт**: привязка по умолчанию `127.0.0.1:8001`, публичный
HTTPS/domain и Instagram URL — T-302. Отдельный T-304 добавит `/health` и мониторинг.
Тест GitHub Actions `.github/workflows/docker.yml` проверяет холодный старт,
миграцию новой БД и сохранность `runtime` при пересоздании контейнера. До
успешного CI и реального VPS-smoke приёмка T-301 не завершена.

**Тома и резервные копии:** `crosspost_runtime` примонтирован как `/app/runtime`
(SQLite вместе с WAL, `media`, `screenshots`, `profiles`, `settings.json`),
`crosspost_backups` — как `/app/backups`. Эти тома содержат клиентские секреты
и никогда не отдаются напрямую веб-сервером. Не выполнять `docker compose down -v`:
это удалит данные и локальные резервные копии. Только SQLite online backup:

```bash
docker compose exec -T app python scripts/sqlite_backup.py backup --db runtime/db.sqlite3 --dest /app/backups --keep 7
```

Бэкап **неполон без** защищённой внешней копии `media`, `profiles`,
`settings.json` и отдельно сохранённого `VAULT_KEY`; их нужно шифровать и
выгружать в независимое хранилище. Перед реальными клиентами провести восстановление
копии БД и медиа по §6, проверку ключа и доступа. Именованные тома на том же
VPS не заменяют внешнюю копию.

**Перезапуск/обновление:** `docker compose restart app` не удаляет тома; сигнал
SIGTERM и `stop_grace_period: 120s` дают Uvicorn время отменить фоновые задачи.
Перед обновлением выполнить backup по §6, сохранить идентификатор предыдущего
образа и проверить совместимость миграций. Для отката остановить приложение,
вернуть прежний образ и **при несовместимой миграции** восстановить прежнюю БД
в новый файл (не затирая старую). Сам по себе откат образа не откатывает схему.

**Важное ограничение:** `BROWSER_HEADLESS=true` позволяет публикации через Chromium,
но вход VK/Яндекс в приложении всегда требует **видимого** окна браузера. Для
удалённого интерактивного входа, Xvfb/VNC и проверки антибота нужна T-303.

## 10. HTTPS и публичные подписанные медиа (T-302, код на ревью)

Этот раздел описывает **будущую** конфигурацию после owner-merge T-301 → T-302,
не свидетельствует о выполненном VPS deployment. У CI нет реального домена,
сертификата ACME или внешнего Meta fetch. Production вводит только владелец.

**Предварительные условия:** FQDN под управлением владельца, DNS A на публичный
IPv4 VPS, AAAA только если публичный IPv6 действительно маршрутизируется к VPS;
внешние TCP **80 и 443** разрешены firewall/VPS-провайдером и не заняты другим
nginx/Caddy, наружу **не** открывать 8001, SQLite, Docker API, VNC или отладку.
В outbound сети серверу нужен доступ к центрам сертификации/ACME и Instagram;
серверное доменное имя должно реально разрешаться в адрес этого VPS.

Перед запуском установить внешнюю переменную `CROSSPOST_DOMAIN` — только
DNS-hostname, например `post.example.com`, без `https://`, слеша, порта или
пробелов. Обязательные `SESSION_SECRET`, `VAULT_KEY`, `MEDIA_URL_SECRET` согласно
§9 остаются в защищённом окружении, **без** записи в Git или журнал команд.
Для production Compose сам задаёт
`INSTAGRAM_PUBLIC_BASE_URL=https://${CROSSPOST_DOMAIN}`, исключая расхождение
origin между Instagram и Caddy. В обычном CLI/dev разрешён отдельный HTTPS
туннель `*.lhr.life`; не использовать его вместо production DNS.

В Compose Caddy публикует только 80/443 и перенаправляет HTTP→HTTPS; cert/key
хранятся в постоянных томах `caddy_data:/data` и `caddy_config:/config`,
обновляются самим Caddy. FastAPI сохранён на `127.0.0.1:${CROSSPOST_PORT:-8001}`
для локальной диагностики и доступен Caddy через приватную Docker-сеть.
Caddy **не монтирует** `runtime`, `backups` и `media`: `/public-media/{token}`
передаётся в FastAPI без rewrite, где уже действуют HMAC/TTL/path confinement.
Caddy access logs выключены, Uvicorn raw access logs тоже, поскольку token в URL
является временным bearer credential. **Глобальный Caddy runtime logger** также
удаляет `request`/URI/заголовки из proxy-error записей (включая 502), сохраняя
статус и диагностическую трассу. CI специально останавливает тестовый app,
проверяет 502 на фиктивном `/public-media/QA_FAKE_BEARER_DO_NOT_LOG` и отсутствие
этого маркера и фиктивного заголовка в `docker compose logs caddy`.
Не включать общий access log и не менять runtime log formatter без повторного
теста отсутствия сырого URL/headers; реальный токен для такого теста не нужен.

**Доверие к прокси:** у Caddy статический IP
`${CROSSPOST_PROXY_IP:-172.30.92.2}` в отдельном bridge subnet
`${CROSSPOST_PROXY_SUBNET:-172.30.92.0/24}`. Для app закреплён отдельный
`${CROSSPOST_APP_IP:-172.30.92.3}`, чтобы app не занял адрес Caddy при старте.
Uvicorn читает
`FORWARDED_ALLOW_IPS` равный только этому IP, **не `*`**; внешний клиент не может
подставить `X-Forwarded-For` или `X-Forwarded-Proto`. Если выбранный Docker subnet
занят, поменять все три значения согласованно и повторить тесты/контроль сетей.
Сторонний CDN/обратный прокси перед Caddy требует отдельного security-review,
нельзя молча включать дополнительные `trusted_proxies`. T-409 отдельно
включит Secure cookie, CSRF и IP rate limit — до этого платных клиентов не пускать.

Проверки владельца после DNS и безопасного server deployment:

```bash
docker compose config --quiet
docker compose run --rm --no-deps --entrypoint caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose up -d --build
docker compose ps
curl -I "http://${CROSSPOST_DOMAIN}/"           # ожидать 308 и Location: https://...
curl -I "https://${CROSSPOST_DOMAIN}/"          # действующий сертификат; GET /client для UI
curl -f "https://${CROSSPOST_DOMAIN}/client" -o /dev/null
docker compose logs --tail=100 caddy
```

Дополнительно проверить извне (не с самого VPS): валидный TLS chain,
доступность только 80/443, отказ на прямой TCP 8001, неизвестный Host/SNI,
отсутствие отдачи `/app/runtime` и медиа-папок, доступность `/health` после
интеграции T-304. Отдельный приватный одноразовый подписанный URL должен дать
GET/HEAD 200 для нужного видео из внешней сети и 404 при истечении срока либо
изменении токена; убедиться, что Instagram действительно может скачать видео.
**Никогда не вставлять полный подписанный URL в issue, Actions, чат или логи.**
В Caddy/приложении убедиться, что path не содержит raw `/public-media/{token}`.

**Продление/откат:** cert volume должен переживать `docker compose down`/
пересоздание контейнера. Проверить автообновление по логам/состоянию сертификата
до истечения срока, отслеживать внешним мониторингом TLS expiry. Два Caddy-тома
вместе с `runtime`, `backups`, media и защищённым ключевым материалом включить
в план *независимых зашифрованных* backup/restore drill. Перед rollback сохранить
образы и резервную копию данных, откатить T-302 только после согласования домена
и доступа к старому приложению; остановка Caddy прекратит публичный HTTPS.
Не выполнять `docker compose down -v` и не удалять сертификатные тома.
