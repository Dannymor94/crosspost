# ARCHITECTURE.md — как устроено и почему

Правила-инварианты — в [`AGENTS.md`](../AGENTS.md) §3. Здесь объяснение и карта кода.

---

## 1. Продукт

Клиент загружает **готовый** контент (текст, фото, видео, статью), выбирает каналы и цели,
публикует сразу или по расписанию и видит результат по каждому каналу. Владелец сервиса
обслуживает подключения и разбирает сбои «под капотом». Каналы ориентированы на аудиторию в РФ.

Пользователи:
- **admin** (владелец, оператор) — все профили, подключения, модерация, консоль ошибок, настройки;
- **client** — один свой профиль: публикация и планирование через мобильный `/client`;
  при включённой модерации посты клиента ждут одобрения.

---

## 2. Главный принцип: граница API ↔ браузер

У площадок разная природа доступа, и от неё зависит всё остальное.

| | API-тир (`adapters/api/`) | Браузерный тир (`adapters/browser/`) |
|---|---|---|
| Доступ | токены, ключи, сессия SDK | живой Playwright-браузер, `storageState` |
| Надёжность | высокая | хрупкий: DOM меняется, антибот |
| Квитанция | площадка возвращает `external_id` | часто нет; проверка по странице |
| Повтор | по идемпотентности | только verify-before-retry |
| Подключение | поля мастера | вход в окне браузера руками |
| Каналы | Telegram, Instagram, Telegraph | Яндекс Бизнес, ВК-стена, ВК-канал |

Стратегия: **переводить каналы из браузера в API везде, где площадка позволяет.**
Каждый переехавший канал перестаёт быть источником проблем.

Почему ВК в браузере: API площадки отказал (ошибки 27, 214 на токене сообщества,
user-токен через официальный поток недоступен).

---

## 3. Конвейер публикации

Один путь для всех каналов, различия — в декларациях:

```
валидация → capability-проверка → рендер под канал → сессия (лок profile+channel)
         → действие → verify → ChannelResult → статус → чистка медиа (когда все финальны)
```

- `orchestrator/publish_service.py` — ядро: раскладывает публикацию на пары
  (канал, цель), вызывает адаптеры, агрегирует результаты, повторяет только упавшее.
- Идемпотентность: `publication_id` генерится до отправки; чекпоинты в таблице `checkpoints`.
  Для планировщика ключ `schedpost-<id>`.
- Частичный успех — нормальный исход.

**Состояния:**
- публикация в канал: `done | failed | skipped | submitted | needs_relogin`;
- подключение: `live | needs_relogin | banned`;
- запланированный пост: включая `PENDING_APPROVAL` (модерация клиента).

---

## 4. Контент

`content/canonical.py` — `CanonicalContent` с типом `post | article | reel | story`.
`content/capabilities.py` — какой канал какие типы принимает и правила по видео
(единый источник для UI, серверного гейта и импорта). `content/article_blocks.py` —
блочные статьи (Telegraph). Рендер под формат канала живёт в файле адаптера.

---

## 5. Данные и изоляция

SQLite через SQLAlchemy async (`db/`). Таблицы (`db/models.py`): `Profile`, `User`,
`Connection`, `Credential`, `ChannelTarget`, `Publication`, `Checkpoint`, `ScheduledPost`,
`Log`, `ProfileSetting`.

- Каждая запись клиента привязана к `profile_id`; репозитории фильтруют по нему.
- Доступ в роутах — через зависимости `web/deps.py` (`require_profile_access`, `require_admin`).
- Учётные данные каналов шифруются в vault (Fernet, `db/vault.py`); ключ `VAULT_KEY` из окружения.
- Медиа отдаются площадкам только по подписанным истекающим ссылкам `/public-media/{token}`.
- Миграции: сейчас Alembic + ручные `ALTER` в `db/engine.py` (K-6, переход в T-201).

---

## 6. Рецепт нового канала

Новый канал не требует правок роутов, `publish_service` и фронта. Нужна одна ветка в фабрике
адаптеров: без неё `build_profile_adapter()` вернёт `None`, и канал не опубликуется.

1. Скопировать ближайший адаптер: `adapters/api/telegram.py` (API) или
   `adapters/browser/yandex.py` (браузер).
2. Скопировать его тест из `tests/adapters/...` → тест канала на моках площадки.
3. Заменить рендер (canonical → формат канала) и вызов отправки.
4. Вернуть `ChannelResult` с квитанцией.
5. Добавить декларацию в `channels/validators.py` (тир, тип учётных данных, мастер из
   `channels/guide.py`, платформа, при необходимости `proxy_env_var`, `account_group`)
   и типы в `content/capabilities.py`.
6. Зарегистрировать создание адаптера в `orchestrator/adapter_factory.py`: ветка по имени канала
   в `build_profile_adapter()` (API) или в `_build_browser()` (браузер). Тест: `build_profile_adapter()` для
   канала с моковыми учётными данными возвращает адаптер нужного класса, публикация через
   `publish_service` на моке даёт `ChannelOutcome`. Декларативная регистрация — T-506.
7. Добавить строку в `docs/STATUS.md` §1 и, при особенностях, `docs/channels/<канал>.md`.
8. Тесты на моках зелёные → канал готов к живому smoke владельца.
9. По желанию — ветка в CLI (`build_adapter` в `__main__.py`), если канал нужен из командной строки.

Контракт (`adapters/base.py`):
```python
async def publish(self, content: CanonicalContent, *, publication_id: str) -> ChannelResult
```

---

## 7. Планировщик и импорт

- `scheduler/executor.py` — фоновый исполнитель в lifespan приложения: забирает созревшие
  посты, публикует с пейсингом, ретраит с потолком, чистит медиа. Флаги `SCHEDULER_*`.
- `scheduler/browser_cooldown.py` — пауза между браузерными сессиями по `(profile, account_group)`:
  ВК-стена и ВК-канал делят один аккаунт и один кулдаун.
- `scheduler/import_plan.py`, `remote_table.py`, `media_sources.py` — импорт контент-плана
  (Google Sheets, XLSX, CSV; медиа из папки, по HTTP, с Google Drive и Яндекс.Диска).
- Время постов — naive local.

---

## 8. Сеть и деплой

- Целевое размещение — VPS вне РФ (Telegram и Instagram ограничены из РФ).
- ВК и Яндекс требуют российский IP → прокси на канал через `proxy_env_var` (для браузера — T-204).
- Instagram забирает видео по публичному URL; в dev — туннель (`tunnel/`, `make tunnel`).

---

## 9. Карта кода

```
src/crosspost/
  adapters/
    base.py              контракт: ChannelAdapter, ChannelResult, ResultStatus
    api/                 telegram, instagram, telegraph, vk (заблокирован), youtube (заглушка)
    browser/             base_browser (open_page, login_context, storage_state),
                         yandex, vk_wall, vk_channel; vk — только для CLI `--to vk`;
                         dzen, whatsapp, instagram — заглушки
  channels/
    validators.py        реестр каналов (декларации)                 ← общий реестр
    guide.py             шаги мастера подключения (декларация)
    telegram_login.py, browser_login.py, telegram_discovery.py, connection*.py
  content/               canonical, capabilities (← общий реестр), validation,
                         article_blocks, article_link, telegram_text, media_kind
  orchestrator/          publish_service (ядро), adapter_factory (← общий реестр), task (идемпотентность),
                         article_flow, breaker (заглушка — T-203)
  scheduler/             executor, import_plan, remote_table, media_sources,
                         browser_cooldown
  db/                    models (← горячая точка), engine (миграции), *_repo, vault, sqlite_store
  auth/                  passwords (scrypt), sessions (HMAC-кука)
  sessions/              health, relogin, vault — заглушки (T-202, T-205)
  tunnel/                туннель для Instagram в dev
  web/
    app.py               create_app(), роутеры (← общая точка сборки)
    deps.py              DI и проверки доступа
    routes/              auth, users, profiles, channels, tg_login, telegram_discovery,
                         publish, import_plan, settings, errors, public_media, tunnel_health
    frontend/index.html  админ-UI, один файл (← горячая точка)
    frontend/client.html мобильный клиентский UI
    signed_media.py, media.py
  config.py              load_config(), флаги
  runtime_settings.py    настройки из админки, переживающие рестарт
  __main__.py            CLI: post, login, user-add, tg-session; свой build_adapter, учётки из env
tests/                   зеркало src/, всё на моках
runtime/                 .env, база, медиа, сессии — не в git
```
