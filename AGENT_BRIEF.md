# AGENT_BRIEF.md — праймер для агента

> Самодостаточный контекст, чтобы новая сессия агента начала работать над фичей
> **без чтения всех доков**. Даёшь агенту: «прочти AGENT_BRIEF.md, делай <фича>».
> Глубже: `PROJECT_GUIDE.md` (почему так), `ROADMAP.md` (куда идём), `PRD_BACKLOG.md`
> (задачи), `CLAUDE.md` (жёсткие инварианты). Не грузить их все разом без нужды.

---

## 1. Что это

Локальный кросс-постинг: клиент грузит **готовый** контент → выбирает каналы →
публикуется во все одним действием → видит **поканальный** результат. Система
**доставляет**, а не генерирует контент. Генерацию не добавлять.

Стек: Python 3.13, FastAPI (async везде), SQLAlchemy async + SQLite, Alembic,
Telethon (TG), Playwright (браузерные каналы). Фронт — один `index.html` на
Preact-подобном `h()` (без сборки, CDN-free, раздаётся FastAPI).

---

## 2. Главный инвариант: граница API ↔ браузер

Каждый канал — по одну сторону границы, **не смешивать**. Это объясняет почти
любое архитектурное решение здесь.

- **API-тир** (`adapters/api/`): stateless, общий IP, площадка возвращает
  квитанцию `external_id`. Telegram, (VK API — заблокирован платформой).
- **Браузерный тир** (`adapters/browser/`): персистентные профили, лок сессии,
  storageState, квитанции нет, `verify-before-retry`. Яндекс, ВК-стена, ВК-канал.

Нельзя: браузерной логики / сессий / прокси в API-адаптере; общего скрипта на
несколько браузерных каналов. Импорт `sessions/`/`profiles/` в `api/` = протечка.

---

## 3. Реальное состояние (на 2026-08-10)

CLAUDE.md `▶ СЕЙЧАС` **устарел** (говорит «Итерация 1»). По факту сделано больше:

| Слой | Что работает |
|---|---|
| Данные (0) | SQLite-схема, Fernet-vault, ProfileRepository (изоляция по `profile_id`), validate_connection |
| Каналы | Telegram (API) ✅, Яндекс / ВК-стена / ВК-канал (браузер) ✅ — постят вживую |
| UI подключений (итер.1) | профили, карточки каналов, интерактивный вход TG, браузер-логин, per-profile target |
| Публикация (итер.2а) | экран публикации, поканальные статусы, ретрай одного канала, планировщик (сохранение) |
| Авторизация | login+пароль → подписанная httponly-кука, роли admin/client, изоляция профилей |

**Не закоммичено** (в рабочем дереве): весь слой авторизации — `src/crosspost/auth/`,
`db/user_repo.py`, `routes/auth.py`, `routes/users.py`, `tests/auth/`. Учитывай при `git`.

Каналы в capability-матрице, помеченные как реализованные адаптеры: `telegram`,
`yandex`, `vk_wall`, `vk_channel`. Остальные (`whatsapp`, `instagram`, `dzen`,
`youtube`, `telegraph`, `vk` API) — в матрице/реестре есть, адаптеров нет.

---

## 4. Карта кода (где что)

```
src/crosspost/
  adapters/
    base.py            — ChannelAdapter (Protocol), ChannelResult, ResultStatus  ← КОНТРАКТ
    api/telegram.py    — Telethon, StringSession
    browser/           — base_browser.py (open_page/login_context/storage_state) + yandex, vk_wall, vk_channel
  content/
    canonical.py       — CanonicalContent, ContentType (post|article|reel|story)
    capabilities.py    — матрица канал → {ContentType}  ← общий реестр
    validation.py      — validate(content) по типу
  channels/
    connection.py      — ChannelConnection (live|needs_relogin|banned)
    validators.py      — VALIDATORS: реестр каналов (title/kind/credential_kind/target/…)  ← общий реестр
    connection_validator.py, browser_login.py, telegram_login.py
  orchestrator/
    task.py            — publication_id (идемпотентность), состояние задачи
    publish_service.py — PublishService.publish/retry_channel (ядро оркестрации)
    adapter_factory.py — build_profile_adapter(repo, profile_id, channel)
    breaker.py, dispatcher.py, queue.py  — заготовки под очередь (пока синхронно)
  scheduler/           — deferred.py, pacing.py (планировщик, пока только хранение)
  db/
    models.py          — ORM: Profile, Connection, Credential, Publication, ScheduledPost, User, Log
    profile_repo.py, publication_repo.py, user_repo.py, vault.py, engine.py
  auth/                — passwords.py (scrypt), sessions.py (HMAC-кука)   [не закоммичено]
  web/
    app.py             — create_app(), регистрация роутеров, раздача SPA   ← ОБЩИЙ ТОЧКА СБОРКИ
    deps.py            — DI: RepoDep/SessionDep/CurrentUser, require_profile_access/require_admin
    routes/            — auth, users, profiles, channels, tg_login, publish  (один файл = одна фича)
    frontend/index.html — весь UI (~1850 строк, один файл)   ← ГОРЯЧАЯ ТОЧКА КОНФЛИКТОВ
  config.py, __main__.py (CLI: post / login / user-add)
runtime/               — .env, db.sqlite3, media/, sessions/  (⛔ в .gitignore, не коммитить)
tests/                 — зеркало src/, всё на моках площадок (28 тест-файлов)
```

---

## 5. Контракт адаптера + рецепт нового канала

Каждый канал реализует **один** интерфейс ([adapters/base.py](src/crosspost/adapters/base.py)):

```python
async def publish(self, content: CanonicalContent, *, publication_id: str) -> ChannelResult
```

`ChannelResult = {channel, status, external_id?, error?}`. `status ∈ {done, failed,
skipped, submitted, needs_relogin}`. `external_id` — **квитанция об успехе**, НЕ ключ дедупа.

**Рецепт добавления канала (TDD, не трогая оркестратор):**
1. Скопировать ближайший адаптер (`api/telegram.py` или `browser/yandex.py`) → новый файл.
2. Скопировать его тест (`tests/adapters/…`) → тест канала на **моках площадки**.
3. Поменять рендер (canonical → формат канала) и вызов отправки.
4. Вернуть `ChannelResult` с квитанцией.
5. Зарегистрировать канал в `channels/validators.py` (VALIDATORS) и `content/capabilities.py`.
6. Зелёный тест на моках = канал готов. Реальные ключи — только для финального smoke.

Новый канал **не требует** правок оркестратора, роутов, фронта (кроме появления в реестрах).

---

## 6. Жёсткие правила (нарушать нельзя — полностью в CLAUDE.md)

1. **Идемпотентность по внутреннему `publication_id`** (генерится до отправки), не по `external_id`.
2. **Браузерный тир — verify-before-retry.** Слепой ретрай в браузере запрещён.
3. **Бан/лимит → circuit breaker.** Забаненный канал не ретраить.
4. **Сырые пароли не хранить нигде.** Релогин — только user-driven (QR/код).
5. **Лок `(profile, channel)`** перед доступом к сессии.
6. **Чистка медиа** — только когда ВСЕ каналы финализировались (+ TTL).
7. **Контент рендерится под канал** и только в поддерживаемые типы (capability-матрица).
8. **Частичный успех — норма.** Ретрай только упавшего канала, с потолком попыток.
9. **Изоляция по `profile_id`** — каждый запрос фильтрует по нему; client видит только свой профиль.
10. **DOM-селекторы и методы SDK не угадывать** — снимать с живой страницы / читать доки.
11. **Ключ vault (`VAULT_KEY`) — снаружи** (env), не под `runtime/`. Нет ключа → явная ошибка.

---

## 7. Как работать параллельно (главное для тебя)

Архитектура заточена под изоляцию — используй естественные швы, избегай горячих точек.

### Швы (фичи, которые агенты делают независимо, почти без конфликтов)
- **Новый канал-адаптер** — свой файл в `adapters/*/` + свой тест. Самый чистый шов.
- **Новый backend-роут / фича в существующем роуте** — один файл в `web/routes/`.
- **Логика в orchestrator / scheduler / db-репозитории** — если фичи трогают разные файлы.
- Тесты зеркалят src → редко конфликтуют.

### Горячие точки (сериализовать или делить аккуратно — сюда сходятся все)
- **`web/frontend/index.html`** — весь UI в одном файле. Два агента, правящих его,
  почти гарантированно конфликтуют. Если фича фронтовая — либо один агент на index.html
  за раз, либо заранее разнести по разным секциям/компонентам и мержить руками.
- **`web/app.py`** — регистрация роутеров. Новый роут = +1 строка `include_router`. Мелко, но общее.
- **`db/models.py` + Alembic** — схема. Миграции от разных агентов конфликтуют по
  ревизиям; параллельные изменения схемы согласовывать.
- **`channels/validators.py`, `content/capabilities.py`** — общие реестры. Новый канал
  дописывает по строке в каждый — конфликт разрешается тривиально, но помни о нём.
- **`CLAUDE.md` и доки** — общий текст.

### Git-стратегия для параллели
- Ветка (или git-worktree) на фичу. Изоляция агента через worktree — если гоняешь
  суб-агентов, у Agent-тула есть `isolation: "worktree"`.
- Держи фичу в своих файлах; горячие точки трогай минимально и последним шагом.
- Перед стартом: `git status` — учти, что слой auth **ещё не закоммичен** (см. §3).

### Быстрая матрица «фича → где живёт → риск конфликта»
| Тип фичи | Основные файлы | Конфликт |
|---|---|---|
| Новый канал | `adapters/*/новый.py`, тест, +реестры | низкий |
| Новый API-эндпоинт | `web/routes/*.py`, +`app.py` | низкий |
| Оркестрация/очередь | `orchestrator/*.py` | низкий-средний |
| Изменение схемы БД | `db/models.py`, `alembic/` | **высокий** |
| Фронтовая фича | `web/frontend/index.html` | **высокий** |

---

## 8. Запуск, тесты, аккаунты

```bash
make install-mvp         # pip install -e ".[dev]"  (ядро + dev, без playwright/celery)
make install             # полная (browser/infra/web/crypto/youtube) + playwright chromium
make test                # pytest -q   (всё на моках, ключи не нужны)
make lint                # ruff check src tests
make run                 # uvicorn crosspost.web.app:app --reload  → http://localhost:8001
```

Сейчас dev-сервер уже поднят: **http://localhost:8001** (uvicorn --reload, порт 8001).

Завести пользователя панели (нужен для входа — регистрации в UI нет):
```bash
python -m crosspost user-add --login admin --role admin --password <pass>
python -m crosspost user-add --login client1 --role client --profile-id 1 --password <pass>
```

CLI-постинг (для smoke, минуя UI):
```bash
python -m crosspost post --type post --text "..." --image path.jpg --to telegram --profile 1
```

Реальные ключи — только для живой отправки (`SETUP.md`). **Mock-first**: весь код
пишется и тестируется на моках, ключи не ждать.

---

## 9. Чек-лист перед сдачей фичи

- [ ] Тесты на моках зелёные (`make test`), новый код покрыт.
- [ ] `make lint` чистый.
- [ ] Не нарушены инварианты §6 (изоляция profile_id, граница API↔браузер, идемпотентность).
- [ ] Не угаданы селекторы/методы SDK — сверено с живой страницей/доками.
- [ ] Секреты не попали в git (`runtime/`, `.env`, `*.session`, vault) — проверить `git status`.
- [ ] Горячие точки (§7) тронуты минимально; при изменении схемы — миграция согласована.
