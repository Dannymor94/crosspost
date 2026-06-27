# PROJECT_STRUCTURE.md

Файловая структура и её связь с архитектурой. Дерево **кодирует границу API↔браузер**:
смешать тиры нельзя, не нарушив очевидно раскладку каталогов.

> v1.1: рендер сведён в файл адаптера (без преждевременного сплита); добавлены `content/capabilities.py`, `channels/` (состояние подключения), `orchestrator/breaker.py`; ключ vault вынесен из `runtime/`.

---

## Дерево

```
crosspost/
├── README.md                  # обзор, запуск, ссылки на доки
├── CLAUDE.md                  # инварианты для агента
├── PROJECT_GUIDE.md           # архитектура (что и почему)
├── PRD_BACKLOG.md             # задачи по эпикам
├── PLAN.md                    # порядок исполнения
├── .gitignore                 # НЕСУЩИЙ: прячет все секреты и runtime
├── .env.example               # шаблон конфигурации (коммитится)
├── pyproject.toml             # зависимости, метаданные пакета
├── docker-compose.yml         # redis, postgres (+ xvfb-воркер позже)
│
├── src/crosspost/
│   ├── config.py              # загрузка .env, пути к runtime/
│   │
│   ├── content/               # ЭПИК 1 — контент-слой
│   │   ├── canonical.py       # CanonicalContent, поле type
│   │   ├── validation.py      # валидация по типу
│   │   └── capabilities.py    # capability-матрица: канал → {type}
│   │
│   ├── adapters/              # КОНТРАКТ + 8 каналов — ПО ГРАНИЦЕ
│   │   ├── base.py            # async publish(canonical) -> ChannelResult; ChannelResult
│   │   ├── api/               # ── API-ТИР ── (рендер + публикация в одном файле)
│   │   │   ├── telegram.py    # Telethon, StringSession
│   │   │   ├── vk.py
│   │   │   ├── youtube.py
│   │   │   └── telegraph.py
│   │   └── browser/           # ── БРАУЗЕРНЫЙ ТИР ──
│   │       ├── base_browser.py # обвязка: профиль, лок, verify-before-retry, детекция бана
│   │       ├── whatsapp.py
│   │       ├── instagram.py
│   │       ├── dzen.py
│   │       └── yandex.py
│   │
│   ├── channels/             # состояние ПОДКЛЮЧЕНИЯ (не задачи)
│   │   └── connection.py      # ChannelConnection: live | needs_relogin | banned
│   │
│   ├── orchestrator/          # ЭПИК 3
│   │   ├── dispatcher.py      # раскладка; проверка connection+breaker перед диспатчем
│   │   ├── queue.py           # очередь, раздельные fast/slow дорожки
│   │   ├── breaker.py         # circuit breaker на (user, channel)
│   │   └── task.py            # состояние ЗАДАЧИ + publication_id (идемпотентность)
│   │
│   ├── media/                 # ЭПИК 7
│   │   └── lifecycle.py       # temp + чистка по финалу ВСЕХ каналов + TTL
│   │
│   ├── sessions/              # ЭПИК 6 — только браузерный тир
│   │   ├── vault.py           # шифрованное хранилище (ключ — снаружи!)
│   │   ├── health.py          # проактивный health-check
│   │   └── relogin.py         # user-driven релогин (QR/код)
│   │
│   ├── scheduler/             # ЭПИК 8
│   │   ├── deferred.py        # отложка
│   │   └── pacing.py          # пейсинг (отложка → пейсинг → очередь)
│   │
│   ├── users/                 # ЭПИК 9
│   │   └── model.py           # пользователь, его токены/сессии/прокси
│   │
│   └── web/                   # ЭПИК 4 — веб-интерфейс
│       ├── app.py             # FastAPI
│       ├── routes.py          # /create, /status, /retry, /connections
│       └── frontend/          # форма + поканальные статусы
│
├── tests/
│   ├── adapters/
│   │   ├── api/               # тесты идемпотентности по publication_id
│   │   └── browser/           # тесты verify-before-retry, лока, детекции бана
│   ├── content/              # тесты capability-матрицы
│   └── orchestrator/         # тесты breaker, частичного успеха
│
└── runtime/                   # ⛔ ЦЕЛИКОМ В .gitignore — НИЧЕГО ОТСЮДА НЕ КОММИТИТСЯ
    ├── .env                   # реальные секреты (НО не ключ vault)
    ├── state.db               # SQLite (только фаза A; дальше Postgres)
    ├── vault/                 # зашифрованные storageState
    ├── profiles/              # browser user-data-dir на пользователя
    ├── sessions/              # Telethon *.session файлы
    └── media_tmp/             # медиа до отправки
```

**Ключ vault НЕ в дереве `runtime/`** — инжектится через переменную окружения / OS keyring / внешний секрет-стор. Если положить его в `runtime/`, копия каталога унесёт и замок, и ключ.

---

## Принципы раскладки

**Граница в каталогах.** `adapters/api/` vs `adapters/browser/` — не косметика. API-адаптер не имеет доступа к `sessions/` и `profiles/`; браузерный — имеет. Импорт сессий/прокси в `api/` = граница протекла.

**Две сущности состояния — в разных местах.** `orchestrator/task.py` — состояние публикации (+ `publication_id`). `channels/connection.py` — состояние подключения (релогин/бан). Их смешение было багом ранней версии.

**Рендер — в файле канала.** Один канал = один файл адаптера, где и рендер, и публикация. Отдельный `Renderer` выделяется, только когда логика рендера начнёт делиться между каналами. Так не плодим 16 файлов на 8 каналов заранее.

**Адаптеры изолированы.** Падение/редизайн одного не трогает соседей. Общая обвязка браузерного тира — в `base_browser.py` (профиль, лок, verify-before-retry, детекция бана), это инфраструктура, а не «скрипт на все каналы».

**Вся изменяемая и секретная дата — под `runtime/`** (кроме ключа vault). Один игнорируемый корень → утечка через гит структурно невозможна.

---

## Пуш на GitHub — порядок и безопасность

> Проект хранит токены и сессии (при друзьях — чужие аккаунты). Утёкший секрет из истории гита считается скомпрометированным навсегда. Порядок жёсткий.

**1. Сначала `.gitignore`, потом первый `git add`.**

```bash
git init
git status                      # в выводе НЕ должно быть .env, runtime/, *.session, vault*
git add .
git status                      # перепроверить список к коммиту глазами
```

Мелькнул секрет — остановиться, чинить `.gitignore`, не коммитить.

**2. Репозиторий — приватный.**

```bash
git commit -m "Каркас: структура, контракт адаптера, доки"
git branch -M main
git remote add origin git@github.com:<user>/crosspost.git
git push -u origin main
```

**3. Коммитится `.env.example`, не `.env`.** Реальные значения — только в `runtime/.env`. Ключ vault не пишется даже туда — он инжектится в окружение.

**4. Если секрет попал в коммит** — не «удалить файл следующим коммитом» (остаётся в истории). Считать скомпрометированным: отозвать/перевыпустить токен, перелогинить сессию; чистить историю (`git filter-repo`) только до пуша, после пуша — лишь ротация.

**5. Опционально:** pre-commit хук / `git-secrets`, блокирующий коммит при совпадении с паттернами токенов.

---

*Структура выведена из PROJECT_GUIDE.md v1.1 и PRD_BACKLOG.md v1.1. Каталоги создаются по мере прохождения эпиков — не обязательно все сразу.*
