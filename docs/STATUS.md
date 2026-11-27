# STATUS.md — текущее состояние

**Единственный источник правды о состоянии проекта.** Меняется в том же PR, что и поведение.

- **Последняя сверка с кодом:** 2026-11-20, integrated main `3b7cd89` включает T-207/T-213/T-202/T-209/T-203; T-210 в ветке на независимом ревью
- **Текущая фаза:** 2 — Надёжность для продакшена (см. [`ROADMAP.md`](ROADMAP.md))
- **Фокус:** надёжность для продакшена — health-check сессий, circuit breaker, медиа submitted-постов, журнал статусов.

---

## 1. Каналы (веб-реестр `channels/validators.py`)

| Канал (ключ) | Тир | Типы контента | Статус | Примечание |
|---|---|---|---|---|
| `telegram` | API (Telethon) | post, article, reel, story; фото и видео | ✅ работает | квитанция = `message_id`; форум-топики; несколько целей |
| `instagram` | API (Graph, Instagram Login) | post, reel; видео обязательно | ✅ работает | нужен публичный URL медиа и выход не с российского IP; токен ~60 дней, автопродления нет |
| `telegraph` | API | article | ✅ работает | связка «статья + анонс в Telegram» |
| `yandex` | браузер | post | ✅ работает | квитанция-заглушка `submitted`; модерация площадки до 7 дней |
| `vk_wall` | браузер | post | ✅ работает | общая сессия ВК с `vk_channel` |
| `vk_channel` | браузер | post | ✅ работает | submitted; реальное подтверждение — T-502 |
| `vk` | API | — | ⛔ отключён в веб-реестре | API заблокирован площадкой (ошибки 27/214), ждёт user-токен |
| `youtube`, `dzen`, `whatsapp` | — | в матрице есть | не реализованы | файлы-заглушки |

Реестры: `channels/validators.py` (декларации), `content/capabilities.py` (типы контента),
`orchestrator/adapter_factory.py` (создание адаптеров).

CLI (`python -m crosspost post --to …`) использует свой набор: `telegram`, `vk_api`, `yandex`,
`vk_wall`, `vk_channel`; учётные данные берёт из env, а не из vault. Старый браузерный
канал `vk` удалён; CLI предлагает использовать `vk_wall`.

---

## 2. Что работает

- **Профили и роли.** `admin` видит все профили; `client` — только свой (`profile_id`).
  Вход по логину и паролю, HMAC-кука. Пользователи заводятся через CLI `user-add`.
- **Подключение каналов.** Пошаговый мастер; шаги и тексты задаются декларацией
  (`channels/guide.py`). Интерактивный вход в Telegram, браузерный вход для ВК и Яндекса.
- **Публикация.** Статусы по каждому каналу, повтор только упавшего, несколько целей на канал,
  видео, статьи (блоки), ссылка на статью с настраиваемой фразой.
- **Планировщик.** Отложенные посты; исполнитель в lifespan приложения публикует созревшие
  посты с пейсингом и потолком попыток; кулдаун браузерных сессий по группе аккаунтов.
- **Импорт контент-плана.** Google Sheets по ссылке, XLSX, CSV; сопоставление колонок;
  медиа из папки, по HTTP, с Google Drive и Яндекс.Диска; регулярный импорт по расписанию.
- **Клиентский режим.** Мобильный `/client` (`client.html`); модерация постов клиента
  (`PENDING_APPROVAL` → одобрить или отклонить с причиной).
- **Диагностика.** Консоль ошибок по всем клиентам, скриншот при сбое браузерного канала.
- **Health-check (T-202).** Фоновая проверка LIVE-подключений; подтверждённо мёртвая сессия переводится в `needs_relogin` через credential blob-CAS. Устаревший результат не меняет новое подключение; `banned` сохраняется. Dispatch закрыт для `needs_relogin` / `banned`.
- **Хранение медиа (T-209).** Автоматическое удаление отключено; безопасная чистка остаётся будущей T-214.
- **Аудит медиа (T-213).** CLI только для оператора: объём `MEDIA_DIR`, связи с запланированными и немедленными публикациями, кандидаты в orphan, крупнейшие каталоги. Только чтение; кандидат в orphan не означает разрешение удалить. Живой запуск на сервере ещё не подтверждён.
- **Безопасность.** Vault (Fernet) для учётных данных; подписанные истекающие ссылки
  `/public-media/{token}` вместо открытой раздачи медиа; редакция токенов в логах.
- **Прокси на канал.** `proxy_env_var` в декларации: `INSTAGRAM_PROXY_URL`, `VK_PROXY_URL`
  (общий для стены и канала), `YANDEX_PROXY_URL`. Браузерные publish, validate, UI/CLI-login
  получают прокси через общий `_get_proxy(channel)`; отсутствие обязательного прокси — ошибка.
- **Подтверждение доставки ВК (T-208).** После клика `vk_wall` и
  `vk_channel` возвращают `submitted`: «отправлено, подтверждение недоступно». Даже
  точный текст и новый относительно снимка ID остаются DOM-кандидатом, а не квитанцией.
  Таймаут/ошибка подтверждения после клика не приводят к failed. Submitted терминален
  и не повторяется автоматически. Ошибки до клика остаются ошибками. Реальные квитанции
  и возврат DONE — T-502 после живого smoke в фазе 3 (см. decisions.md).
- **Журнал T-210 (ветка на ревью).** Общий `submitted` не считается `done`, показан
  в «Требуют внимания» вместе с частичными/ошибочными отправками. После исчерпания
  ограниченной DOM-проверки двух VK-адаптеров сохраняется безопасный код `no_match`;
  произвольная ошибка проверки не классифицируется как сеть и не попадает в БД.
  Изменения не добавляют автоповтор или кнопку ручной переотправки SUBMITTED.
- **Сохранность медиа (T-209).** Финализация, retry, cancel, reject и PATCH не удаляют принятое медиа; автоматическая чистка T-214 остаётся выключенной.
- **Health-check (T-202).** Подтверждённая потеря авторизации демотирует только прежний credential; сеть не доказывает потерю сессии, BANNED не снимается автоматически.

- **Circuit breaker (T-203-R1, смержен в main).** Все три пути отправки используют guard подключения,
  затем persistent breaker и атомарную HALF_OPEN lease, затем попытку. OPEN/busy не создают
  адаптер и не меняют публикацию. Только DONE с квитанцией закрывает собственную пробу;
  SUBMITTED остаётся терминальным без подтверждения здоровья. Истёкшая зависшая проба
  уходит в карантин до ручной успешной проверки подключения. Таймер не снимает BANNED.
  R2 закрывает отсутствие Connection при подтверждённом бане и гонку демоции перед учётом
  попытки: условный SQL сохраняет credential fencing, статус/счётчик при отказе и освобождает lease.
  Интеграция #26 прошла независимый QA и PUSH CI после squash-merge всех PR.
  Матрица сигналов и CB-тестов: [`T-203-R1.md`](T-203-R1.md).

---

## 3. Здоровье кода (main после интеграции #26; T-210 в ветке на ревью)

| Проверка | Результат |
|---|---|
| `pytest -q` | integrated main `3b7cd89`: **1265 passed, 2 warnings**, PUSH CI [44707232663](https://github.com/Dannymor94/crosspost/actions/runs/44707232663) Python 3.11/3.13 ✅. T-210 ожидает собственного PR CI; живой бэкап/restore drill не выполнен, production-ready не заявляется |
| `ruff check` / `ruff format --check` | чисто |
| `mypy src` | чисто |
| CI (GitHub Actions) | зелёный: матрица 3.11 + 3.13, `.[dev,browser,infra,crypto]`, `make check` (T-101) |

---

## 4. Известные проблемы

| ID | Проблема | Задача |
|---|---|---|
| K-1 | ~~CI ставил неполные зависимости и использовал Python 3.11~~ — исправлено в T-101 | T-101 ✅ |
| K-2 | ~~`load_config()` не читала переменные вне `_KNOWN_KEYS` из окружения~~ — исправлено в T-102 | T-102 ✅ |
| K-3 | ~~3 теста падают без папки `runtime/`~~ — исправлено в T-103 | T-103 ✅ |
| K-4 | ~~`.env.example` не соответствует коду~~ — исправлено в T-104 | T-104 ✅ |
| K-5 | ~~`web/__main__.py` отсутствовал; `make run` без порта~~ — исправлено в T-105 | T-105 ✅ |
| K-6 | ~~Две системы миграций~~ — запуск и CLI используют Alembic; старые БД обновляются до `head`, включая сохранение `CASCADE` после старого ручного rebuild; схема всех таблиц сверяется с моделями | T-201 ✅ |
| K-7 | ~~T-203 persistent breaker на ревью~~ — смержен с CB-01…CB-18 после независимого QA и CI 3.11/3.13 | T-203 ✅ |
| K-8 | ~~Решение о российском прокси для ВК и Яндекса не реализовано для браузерного тира~~ — реализовано в T-204 | T-204 ✅ |
| K-9 | Мёртвые заглушки: `adapters/browser/instagram.py`, `sessions/vault.py`, `orchestrator/dispatcher.py`, `queue.py`, `media/lifecycle.py` | T-205 |
| K-10 | ~~`TG_API_ID` vs `TELEGRAM_API_ID`~~ — исправлено в T-106; `TG_API_ID` — deprecated alias с предупреждением | T-106 ✅ |
| K-11 | `index.html` ~4800 строк — горячая точка конфликтов | T-501 |
| K-12 | Комментарий TODO про кнопку публикации в `vk_wall.py` при работающем канале; проверить, устарел ли он | T-206 |

## T-207 — резервное копирование (ветка на ревью)

Код `scripts/sqlite_backup.py` и тесты добавлены в отдельном PR: online SQLite WAL snapshot,
проверка SHA256/HMAC и `VAULT_KEY`, восстановление только в новый файл, контролируемая
ротация. Боевой бэкап и операторское восстановление реальной копии ещё **не выполнялись**;
production-ready не заявляется. Подробности: [OPERATIONS.md §6](OPERATIONS.md).

## UX 2.0 — планирование (2026-11-24)

Аудит исходников на main 58dedfcb выявил задачи по модерации, request idempotency, timezone, черновикам, состояниям ошибок и истории. Владелец согласовал направление, включая будущего Telegram Companion. [UX_ROADMAP.md](UX_ROADMAP.md), [UX_CONTRACTS.md](UX_CONTRACTS.md), [UX_DESIGN_SYSTEM.md](UX_DESIGN_SYSTEM.md), [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md). Это только план; код, интерактивный прототип, browser/device smoke и production readiness не подтверждены. Статусы исполнения — BACKLOG. Незавершённые PR и исходные production gates сохраняются.

## T-218 first slice (draft)
Client/admin target intersection, stale request fence and error/empty distinction; no auto-expansion of targets. Shared browser regression added. Draft persistence design only, no durable media or text storage yet. Integration with T-215/216/217 and independent QA pending.

### T-218 second slice (draft)
Private server-side draft API, scoped identity and revision CAS. No local text storage, no media bytes/URLs. UI autosave not yet wired. Migration integration and auth lifecycle review required. CI pending.

### T-218 third slice (draft)
Both editors load/autosave scoped drafts with acknowledged revisions, conflict handling, media reselection and navigation warning. Actual browser lifecycle and exact send/clear integration remain pending. CI/QA pending.

### T-218 fourth slice (draft)
Exact revision clearing on confirmed publication or scheduling receipt, preserve partial/unknown and changed snapshots. Regression tests added; formatter fix; CI, full browser lifecycle and T-216 integration pending.

### T-218 CI regression fix
1293 passed / 3 failed at d7482b67; pure feedback helper no longer depends on browser globals. Added empty/unknown/deferred regression cases. New CI pending.

## T-217 — первая безопасная часть (draft)
Create/PATCH use one offset-to-legacy-local helper, avoiding silent offset stripping on new writes. Existing naive rows unchanged. Added explicit UTC instant/DST primitives and read-only active-queue inventory on a backup. UTC storage cutover, profile zone, import, report and frontend still open; no production audit or migration executed. See [T-217.md](T-217.md). CI/QA pending.

### T-217 second slice (draft)
Opt-in UTC column and API contract, UTC due comparison and time-fenced claim. No rewrite of existing rows. Fixed audit type annotation. CI pending; T-216 migration graph reconciliation and legacy review required before activation.

### T-217 third slice (draft)
Profile IANA timezone setting (admin write, scoped read), server wall-time resolution, DST rejection and UTC editor create/PATCH. Read-only audit uses SQLAlchemy. Import, full mixed-list display and integration remain open. CI/QA pending.

### T-217 fourth slice (draft)
Explicit-zone import preview/commit with UTC resolution and row-level DST/past rejection. Fixed previous formatter findings. Legacy import remains compatible. No immutable preview binding or batch idempotency yet; CI/QA pending.

## T-215 — серверный guard (первая часть, PR draft)

Для клиента при включённой модерации запрещены прямой publish, retry, publish-now расписания, PATCH расписания и commit импорта. Поддерживаемый путь — создание scheduled с PENDING_APPROVAL. Администратор не может вручную запустить pending до approve. Это первая часть задачи, не полная приёмка: CAS отмены, границы гонок при смене настройки модерации, UI и проверка всех переходов остаются открыты. CI и независимый QA обязательны. Не изменяет retry SUBMITTED или retention медиа.

### T-215 follow-up (draft)
CAS отмены сохраняет CANCELLED и медиа; claim и отмена взаимоисключающие. Исполнитель перечитывает снимок после claim, publish-now возвращает 409 при проигранном claim. Клиент читает только scoped настройку модерации и скрывает запрещённые действия; неизвестная настройка блокирует отправку. Политика применяется к новым запросам при проверке на входе; изменение настройки не отзывает уже допущенную операцию. Ретроактивная модерация существующей очереди не реализована. CI и независимое QA pending.

## T-216 — persistent admission ledger (draft, first slice)

Added isolated reservation repository and additive Alembic migration; one winner, stable publication ID, scoped conflict, replay never reacquires after restart. No HTTP route or UI wired yet. No guarantee against current production duplicates until integration and QA. See [T-216.md](T-216.md). Retention policy, scope lifecycle, completion/reconciliation and atomic scheduled insertion require review before activation.

### T-216 second slice (draft)
Opt-in immediate HTTP admission via Idempotency-Key, with generic replay conflict and no redispatch. Legacy API without key, scheduled, and client UI not protected by this slice. Result recovery still open. CI and integration with T-215 pending.

### T-216 third slice (draft)
Read-only recovery with separate secret and bound scope; durable safe result summary, unknown if no recorded outcome. No resends, no changes to adapter retry policy. Requires CI/QA; scheduled and UI not integrated.

### T-216 fourth slice (draft)
Keyed scheduled creation: reservation, post and initial receipt commit together; pre-commit rollback of both rows. Read-only creation receipt recovery. Legacy calls unchanged; keyed external URL media not supported yet. Direct-upload staging may retain orphan media. CI/QA pending.

### T-216 fifth slice (draft)
Both editors use shared operation guard with local pending proof, browser lock and read-only recovery. No automatic resend. Unknown operation remains blocked; no admin resolution yet. Direct-upload-only keyed scheduling, import and legacy API not covered. CI/QA pending.

## UX-01 integration rehearsal — T-215 + T-216

Separate validation-only branch, not for merge. Inputs: PR #53 HEAD 917f38cd and PR #54 HEAD 31d98d2d, both CI green individually. Combined client guards and backend routes, retained cancellation CAS and operation ledger. Added keyed moderation integration regression. CI pending; owner-assisted recovery, import, mandatory rollout and independent QA remain open. Do not deploy or merge this rehearsal.

## UX-01 integration rehearsal — T-215 + T-216 + T-217
Validation only; migration chain t216_operation_result → t217_schedule_utc, canonical UTC inside keyed schedule transaction. CI and independent QA pending; legacy migration, batch import idempotency and unknown-operation resolution remain open. Do not merge or deploy this rehearsal.

## UX-01 browser acceptance rehearsal
Integration 40581ba2 passed CI (run 45781351710); T-218 3dba39f0 passed CI (45781133269). Added dedicated Chromium client-page scenario with pinned Preact/HTM assets and mocked HTTP, no real credentials or external publishes. Exercises actual client.html with draft CAS, reload, missing attachment, unknown send and pending guard. Separate workflow must pass; admin full browser and live owner smoke still pending. Validation branch only; do not deploy or merge.

### Client browser evidence
Dedicated mocked client browser acceptance passed at 4d2c629b (run 45790819952). General CI failed on formatting of the new test (45790819940); formatter fix and screenshot artifact added. Artifact `ux-client-preview` is generated only by the new run, with synthetic client data. Open Actions run → Artifacts → download ZIP → open PNGs. No hosted interactive preview or production deploy. Admin browser E2E and independent QA remain pending.

### Screenshot run recovery
Run 45812196139 failed before browser execution: stale Ubuntu package URLs returned HTTP 404. General CI 45812195771 also stopped on test formatting. Pin rehearsal runners to Ubuntu 24.04 and refresh apt indexes before Playwright dependency installation; simplify formatting-sensitive test statements and print formatter diff before checks. Screenshot archive still unverified until new run passes. No changes to main or production.

### Desktop browser rehearsal
Client browser run 45881474479 passed and artifact 15147880520 was reviewed by owner. General CI 45881474573 stopped on three formatter findings; applied exact diff. Static audit exposed missing admin recovery banner and content-switch selection reset in merged editor; restored them. Added desktop Chromium test of actual admin page, test-only dependency substitution to pinned React UMD, synthetic API, saved draft reload, UTC schedule lost response, recovery unknown and persistent block. No real platform access. New CI/browser runs pending; admin screenshots separate `ux-admin-preview`. Validation branch only, no merge/deploy. Independent QA and remaining UX-01 production gates still open.

### Desktop browser finding (UX-01)
General CI 45884359516 passed at 05600b97. Client browser passed, but desktop scenario 45884359462 failed because target loading never transitioned to ready: merged loadTargets retained the legacy success/error handler. Restore current-request fencing, eligibility intersection and ready/error transitions. Correct test expectation to actual admin selection key telegram plus separator (U+241F). Failure screenshots enabled. New CI and browser run pending; independent QA remains required; no main or deployment changes.

## UX-01 R3 — технический допуск исправлений, 2026-11-27

На validation-only HEAD `83912d1` exact-head CI зелёный, 1353 теста на каждой версии Python; synthetic Chromium client/admin 5/5 каждый; независимый targeted APPROVE и 3 adversarial API probes. Пять находок закрыты, а не весь UX-01. Подготовлен draft delivery-кандидат с сохранением проверенного кода; исходные PR не закрыты. [Матрица поставки и открытые gates](UX01_DELIVERY.md). Main не менялся, production deploy запрещён до отдельных допусков.

## UX-02 visual rehearsal — актуальная основа R3

Delivery UX-01 #59 HEAD `97358b6`: general and mocked editor CI PASS. На отдельной validation-only ветке использованы актуальные shared assets T-501 `087a0a6` и только presentation changes старой визуальной репетиции `ba3f3c2` (не её устаревшие редакторы). Обе действующие страницы используют токены и выбор темы; существующие операции, черновики, маршруты и роли сохранены. Все десять сценариев actual editor acceptance остаются. Новая CI/visual evidence pending. Это промежуточный skin, не полный UX-03 и не production release; исходные PR и main не изменены.

## UX-03 T-510 — первый пакет навигации (validation only)

UX-02 visual CI at `5c84a4e` passed. Shared navigation mounted for actual admin/client, desktop/mobile; profile/client creation, moderation, import, channel and diagnostic views remain reachable. Overview and client home only show real available actions; no fictitious counters or complete history/calendar. Default editor kept for compatibility; full browser history, navigation/device acceptance and rollout remain open. No publish handlers, backend, schema or production changes.

### T-510 navigation history (draft)

Browser scenario 45979009846 passed 6/6 for each role at `59edbce`. Shared history now uses per-mount/context epoch and screen ID only; back/forward runs draft guard, rejected pop restores original entry, old profile/account entries do not restore context. Admin profile selector resets on refusal and profile-list responses fenced on auth lifecycle. New mocked checks cover history and profile refusal/acceptance; CI pending. No changes to backend auth revocation, draft admission, or deployment gates.

### T-510 overview data (draft)

Both role home screens now read scoped active schedule + timezone, display actual scheduled/pending counts and first three server-ordered rows. Explicit loading, empty and error/retry; scope/attempt fencing and abort prevent stale profile data. Legacy wall time labelled, not reinterpreted. No history or global metric invented. Existing default editor and API unchanged. CI pending.

## UX-04 T-511 — shared approximate preview (draft)

First presentation-only slice on T-510 candidate `79bcd63`. Both editors use the same local preview of text/title/selected targets and attachment state. No URL fetch, no HTML injection, no permission or submission logic changes; preview is optional and approximate. Full common editor, upload and target UX are open. Stacked draft; no merge/deploy. CI pending.

## UX-05 T-512 — calendar read model (draft)

Shared pure calendar model for explicit profile IANA zone, day/week/month/list periods and scheduled/pending filter. Canonical times require an explicit offset and valid zone; legacy rows remain separate, not placed on a guessed calendar day. No reschedule/write, UI route or API changes yet. Stacked on first T-511 candidate, CI pending.

### T-512 calendar UI (draft)

Read-only shared calendar mounted in both actual hosts, with day/week/month/list, profile zone, status filter, explicit unplaced records, retry and links to existing editor/management. Admin mobile calendar is primary; moderation remains in More. No new mutation or date prefill. Existing client schedule management retained separately. CI pending.

## UX-06 T-513 — history read model (draft)

New profile-scoped GET history combines persisted scheduled rows and observed immediate publication groups, stable creation-time/source/key cursor, no raw error/external ID/media path or proof. Existing scheduled lifecycle + observed channel outcomes, never infer complete receipt from observed rows. Immediate text absent in current data; ledger-only admissions deliberately excluded pending profile-incarnation fencing and retention policy. No schema or mutation changes. API tests pending; UI, search, complete durable immediate metadata and final acceptance remain open.

### T-513 history UI (draft)

Shared read-only History screen for both roles; source filter, explicitly loaded-only search, cursor pages, error/retry and per-target observed status. No retry controls. Client via More, admin via sidebar/More. Existing incomplete-history limits shown. CI pending.

## UX-08 T-515 — status and preparation (draft)

Client can read own profile's channel statuses and declared preparation steps; no new credential/connection mutations or secret collection. Admin distinguishes banned/unknown from not_connected and does not offer normal reconnect/reset/validate for blocked/unknown state. Existing wizard remains for allowed states. Status is a saved snapshot, not live verification; transient health contract stays server-side. Self-service connection, role/security gates, real login and device acceptance remain open. CI pending.

## 2026-11-27 — последовательный цикл, checkpoint

- #62 T-512 exact code HEAD `0f361c4`: CI PASS, mock Chromium 7/7 client + 7/7 admin (46011491333/46011491349). Исправлена доступная подпись фильтра, сетка по дням недели. Direct create/reschedule ещё открыты.
- #64 T-513 code HEAD `a760e02`: Chromium 8/8 + 8/8 (46012148346), Python 3.13 general CI 1360 passed, 16 skipped, 2 warnings (46012148441); 3.11 на момент записи ещё выполняется. API scope/tie-pagination regression included. История не полная: immediate metadata/ledger-only и server search open.
- #65 T-515 code HEAD `78b2e2a`: клиентский status/preparation screen и admin blocked/unknown guard, тесты обеих ролей добавлены. Exact-head проверки выполняются (46012482227/46012482268). T-514 не дублировать до согласования #33/#36; self-service flow требует auth/role gates.
- #63 правило последовательного автономного цикла: CI PASS, ещё не смержено. Ветки stacked draft; независимый QA и delivery gates не отменены. Работа через GitHub connector, полного локального DoD не заявляем.
