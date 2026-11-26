# Architecture decisions

Short dated entries. One paragraph each.

---

## 2026-11-20 — Причина неполного verify и журнал SUBMITTED (T-210)

`SUBMITTED` остаётся терминальным «отправлено, но не подтверждено», даже если ВК
показал DOM-кандидата с ID; подтверждённый `DONE` определяется более сильной
квитанцией (T-208/T-502). Журнал выделяет `submitted` и `in_progress`, не
объединяя их с `done` или `failed`. После исчерпания ограниченного опроса ВК
возвращает типизированный `VerifyFailed("no_match")`. Произвольные исключения
после клика означают только неизвестность: никакой ложной причины `network`.
На границе `PublishService` `SUBMITTED.error` проходит строгий allowlist
(`no_match` либо `NULL`) перед записью в `Publication.error` и API-ответом.
Схема БД и политика повторов не меняются; T-212 остаётся заблокированной
до отдельного решения владельца. Контракт: [Issue #27](https://github.com/Dannymor94/crosspost/issues/27).

## 2026-11-18 — Persistent rate-limit breaker отдельно от ConnectionState (T-203-R1)

Нормативный контракт — [Issue #22, CB-01…CB-18](https://github.com/Dannymor94/crosspost/issues/22#issuecomment-6990357156). Порядок отправки: DONE/SUBMITTED идемпотентность → T-202 BANNED/NEEDS_RELOGIN guard → T-203 атомарная lease → browser cooldown/разрешённая попытка. Rate limit определяется только типом SDK/HTTP 429, а не строкой ошибки или FAILED. Постоянный подтверждённый бан блокирует подключение до проверенного ручного восстановления; прежнее «бан до кулдауна» из BACKLOG уточнено этим правилом. Generic failure threshold удалён: временная сеть не доказывает лимит. Ключ breaker — `(profile_id, channel)`, browser cooldown по `account_group` сохранён. Один SQL UPDATE с предикатом истечения выдаёт HALF_OPEN и новый epoch; результат принимается только в своём epoch и до срока lease. Первый переход сериализует конкурентные rate-limit результаты: устаревший результат возвращает `False`, не меняя ни состояние, ни счётчик; счётчик принятых результатов инкрементируется SQL-выражением. DONE с квитанцией закрывает только собственную пробу, SUBMITTED/SKIPPED без квитанции не являются подтверждением здоровья. Истечение lease не доказывает остановку удалённой отправки: автоматической второй пробы нет, вместо неё OPEN без deadline (карантин); владелец должен остановить зависшую отправку и явно успешно проверить подключение. Проверка фиксирует epoch до обращения к площадке и не сбрасывает более новый breaker или активную lease. Это безопасная политика восстановления без угадывания состояния удалённого процесса. T-202 blob-CAS сохранён; результаты отправки также не демотируют новое credential. Сырые исключения/ответы площадки не сохраняются в диагностике, только категории. Интегрированы утверждённые T-209/T-202 в ветку T-203; исходные PR не изменены и не смержены. Дальнейшая интеграция с main — по #26, мерж/деплой только владельцем.

Уточнение реализации R2 (2026-11-19), без изменения контракта: точка авторизации попытки — один `INSERT ... SELECT WHERE NOT EXISTS(blocked Connection) ON CONFLICT UPDATE` со счётчиком и ATTEMPTING; DONE/SUBMITTED исключены из UPDATE. Демоция, закоммиченная до этого запроса, запрещает запись. При отказе публикация сохраняется, неиспользованная проба освобождается. Legacy credential без Connection остаётся допустимым; подтверждённый ban/auth записывается условным upsert Connection. Предикат credential blob применяется к SELECT для обеих ветвей (INSERT/UPDATE); UPDATE разрешён только из LIVE, поэтому старый результат не блокирует новое credential и не снимает существующую блокировку.

## 2026-11-17 — ChatGPT назначен главным разработчиком crosspost

Владелец расширил роль ChatGPT с независимого ревьюера до главного разработчика и координатора. ChatGPT самостоятельно выполняет свободные задачи (код, тесты, документация, GitHub Actions), назначает работу другим агентам и ведёт задачи, ветки и PR. Уже назначенную агенту реализацию не дублирует. Код любого автора проходит отдельный независимый QA: автор не утверждает собственную работу. Рутинные commit/push/PR/CI идут без повторного разрешения; мерж и production-deploy по действующему процессу остаются за владельцем. Эта запись заменяет распределение ролей от 2026-11-03, не меняя технические инварианты.

## 2026-11-11 — Автоматическое удаление медиа отключено (T-209)

Три раунда ревью PR #15 показали, что условий безопасного удаления накапливается больше, чем удавалось описать за один проход: submitted-каналы, shared-каталоги (Яндекс checkpoint, общие прикреплённые файлы), orphan после hard-DELETE строки `scheduled_posts`, некорректная метка времени (`updated_at` не обновляется raw SQL upsert), ретраемый FAILED во веб-контексте, потеря `media_key` при PATCH с заменой медиа. Каждый раунд закрывал одну группу условий и открывал следующую. Потеря контента клиента дороже места на диске: пока условия не выведены из реальной картины хранилища, безопаснее не удалять. Автоматическая чистка выведена в T-214 (фаза 3) и будет возвращена после того, как T-213 даст понимание реальных orphan-каталогов, а все шесть условий будут покрыты mutation-killing тестами.

## 2026-11-04 — Alembic как единственный владелец схемы (T-201)

Приложение выполняет `upgrade head` через `AsyncConnection.run_sync`; CLI использует то же окружение Alembic. Ревизии поставляются внутри пакета и не импортируют изменяемые ORM-модели. Базы старого приложения без версии штампуются на `85b6d4dfff8f` после проверки базовых таблиц, колонок и уникальных ключей, затем ревизия `t201_unify` применяет пропущенные ручные изменения. SQLite обновляется в явной транзакции `BEGIN IMMEDIATE`: DDL входит в откат, старты на одном файле сериализуются. Преобразование статуса `published` необратимо, поэтому downgrade этой ревизии явно запрещён; откат — восстановление резервной копии. Миграция требует online-соединения для распознавания старой схемы.

## 2026-11-03 — Переход на рельсы продакшена

MVP пройден: шесть каналов, планировщик, импорт плана, клиентский режим. Следующая цель — сервис для многих клиентов, работающий на сервере без ноутбука владельца. Приоритеты меняются: надёжность, изоляция клиентов и предсказуемость важнее новых каналов. Порядок работ — фазы в `docs/ROADMAP.md` с проверяемыми критериями; новые каналы отложены до фазы 5.

## 2026-11-03 — Два агента с разделёнными ролями

Claude (Claude Code, Sonnet 4.6) — исполнитель: пишет код, тесты, доки, открывает PR. Codex — ревьюер: проверяет PR по чек-листу и выносит вердикт, код в ветку не пушит. Мержит владелец при зелёном CI, `APPROVE` и отсутствии замечаний `blocker`/`major`. Разделение даёт независимую проверку: автор кода не принимает собственную работу. Процесс — `docs/WORKFLOW.md`.

## 2026-11-03 — Документация: один источник правды на каждый вопрос

Правила живут только в `AGENTS.md` (его читает Codex; `CLAUDE.md` подключает его через `@AGENTS.md`). Состояние — только в `docs/STATUS.md`, задачи — только в `docs/BACKLOG.md`. Старые документы этапа MVP перенесены в `docs/archive/` без правок. Причина: девять документов описывали состояние по-разному и отстали от кода, агенты получали ложную картину. Статус и доки меняются в том же PR, что и поведение; согласованность проверяется тестом (T-107).

## 2026-10-28 — Instagram via API tier with video_url and signed links

Instagram is implemented as an API-tier adapter (no Playwright). Meta's video ingestor fetches the clip from a signed `/public-media/{token}` URL rather than via resumable upload. Resumable upload (`upload_type=resumable`, rupload.facebook.com) is unavailable for Instagram Login tokens — confirmed by live API probe. The signed-URL approach keeps the adapter stateless and avoids multipart streaming complexity. The token is HMAC-SHA256-signed and expiring; path confinement is enforced server-side.

## 2026-10-28 — Removed static /media mount; replaced with signed single-file links

The `/media` static mount was public and unguarded — any path under `MEDIA_DIR` was accessible without authentication. It is replaced by `/public-media/{token}` where the token is an HMAC-signed, expiring reference to a single relative path. Path confinement is enforced server-side (`.resolve().relative_to(media_root)`). The full token is never written to access logs (redacted to first 8 chars). This closes the information-leakage risk from publicly browsable media directories.

## 2026-10-28 — Per-channel egress proxy

Channel declarations (`ChannelValidatorDef`) carry a `proxy_env_var` field naming the env var that holds that channel's proxy URL. `adapter_factory` reads this var at construction time and passes `proxy=` to the adapter's HTTP client. This lets Instagram use a non-Russian proxy while VK/Yandex use a Russian one, with no shared proxy state in the orchestrator and no cross-channel interference.

## 2026-10-28 — Browser cooldown keyed by account group, not channel

The scheduler's browser-session cooldown is keyed by `(profile_id, account_group)` rather than `(profile_id, channel)`. Channels with the same `account_group` share a single underlying browser account (e.g. VK wall + VK channel both use the VK account), so one cooldown bucket covers them all. This is closer to how platform rate-limits actually work.

## 2026-10-28 — Deployment direction: VPS outside Russia

Instagram Graph API calls are blocked or severely throttled from Russian IP ranges. Telegram has been throttled from Russia since early 2026. The primary deployment target is a VPS located outside Russia; no proxy is needed for Instagram or Telegram in that case. VK and Yandex require a Russian IP; they are served via the per-channel proxy mechanism — `VK_PROXY_URL` / `YANDEX_PROXY_URL` route their requests through a Russian proxy while Instagram and Telegram use the VPS's own IP directly.

## 2026-10-28 — Dannymor94/ig-probe is a manual diagnostic tool only

The `ig-probe` GitHub Actions repo (`Dannymor94/ig-probe`) was created during development to diagnose Instagram Graph API access from a non-Russian IP. All workflows use `workflow_dispatch` only and are never triggered on a schedule. GitHub Actions terms prohibit using Actions as a general-purpose compute platform; the repo must not gain a `schedule:` trigger or be invoked from production code. Secrets (`IG_TOKEN`) are set immediately before each run and deleted immediately after.


## 2026-11-05 — VK: submitted без реальной квитанции (T-208, T-502)

`vk_wall` и `vk_channel` после выполненного клика возвращают `submitted`: «отправлено,
подтверждение недоступно». Ошибки до отправки остаются ошибками. Недоступное чтение
ленты и таймаут подтверждения после клика не превращают результат в failed.

Точное сравнение нормализует пробелы и переносы и сравнивает текст целиком.
Новый относительно снимка ID и совпавший текст — необходимые признаки кандидата,
но DOM не доказывает его происхождение: старый пост может отрисоваться позднее.
Поэтому текущая реализация сохраняет даже такого кандидата как submitted; DONE
зарезервирован для однозначной идентификации реальной квитанцией. Стабилизация
ленты остаётся эвристикой и не гарантирует новизну. Recovery по точному тексту
также возвращает submitted.

Как у Яндекса, submitted терминален: checkpoint по внутреннему publication_id
предотвращает автоматическую повторную отправку. Это не подтверждение публикации.
UI показывает нейтральное состояние, без зелёной галочки и ошибки. Реальные ошибки
до клика повторяются через verify-before-retry.

Точное подтверждение для VK вернётся после живого smoke владельца в фазе 3,
в рамках T-502 (реальные квитанции); новые DOM-селекторы/форматы ответов не угадываются.

## 2026-11-24 — Crosspost UX 2.0, Web first, Telegram later

Владелец согласовал направление UX 2.0 и включение будущего Telegram-бота, но не его текущую реализацию. Единый backend/БД/планировщик, разделение admin/client, mobile-first, общая дизайн-система, модульность без обязательного переписывания frontend. Критические инварианты до UI-функций: server-side moderation, durable request idempotency, timezone contract, сохранность черновиков. T-212 не разблокируется; T-209 сохраняется. Telegram Companion и optional Mini App только после основного Web и отдельного согласования, те же права и guards. Детали и критерии: [UX_ROADMAP.md](UX_ROADMAP.md), [UX_CONTRACTS.md](UX_CONTRACTS.md), [UX_ACCEPTANCE.md](UX_ACCEPTANCE.md). Цвета, внутренние persistence/time миграции, индивидуальные тексты и расширения API уточняются в своих задачах; production gates не меняются.

## UX-02 — общие ресурсы без обязательной сборки (T-501, первый пакет)
В соответствии с UX_DESIGN_SYSTEM.md и прототипом #52 CSS namespace `.cp-ux`/`--cp-*`, pure JS theme controller в shared/. Хранится только предпочтение темы (не данные аккаунта), system следует matchMedia, недоступное localStorage не препятствует работе. Ресурсы включаются в package-data. Не мигрируем React/Preact и не меняем API/действующие страницы в этом пакете; подключение после QA UX-01. CSS namespace позволяет поэтапно переключать компоненты. Не заявлять новый UI или полное закрытие T-501 до подключения и проверок.
