# Crosspost UX 2.0 — приёмка

План проверок, не результаты тестов. Базовые DoD и независимый QA по [AGENTS.md](../AGENTS.md), [WORKFLOW.md](WORKFLOW.md). Применять [UX_CONTRACTS.md](UX_CONTRACTS.md), [UX_DESIGN_SYSTEM.md](UX_DESIGN_SYSTEM.md).

| Gate | Проверяемое поведение |
|---|---|
| UX-00 | Изолированный прототип, обе роли, темы, все основные маршруты и состояния, нет реальных вызовов; владелец согласует сценарии |
| UX-01 / T-215 | Моки адаптера: client с обязательной модерацией не может вызвать send через каждый маршрут; pending не исполняется до approve; CAS reject/cancel/patch/approve/dispatch, чужие ID denied; корректные действия и сообщения |
| UX-01 / T-216 | Повтор/параллельный запрос, потеря ответа до/после side effect, crash/restart, изменённый payload, другой profile/actor; не более одной логической операции; неизвестный итог не пересылается |
| UX-01 / T-217 | Create/PATCH/import/executor/report round trip, timezone отлична от сервера, DST gap/fold, переход даты, прошлое время, legacy migration dry-run/restore; неизменный instant |
| UX-01 / T-218 | Draft reload/switch/logout, media loss explicitly shown, stale response, changing video preserves intended selection, empty vs error, zero outcomes never success |
| UX-02 | Общие компоненты, темы, клавиатура/контраст, существующие / и /client, packaging и синтаксис, no external CDN dependency for critical bootstrap where feasible |
| UX-03 | Все маршруты обеих ролей доступны, выбранный профиль виден, переключение безопасно, browser back/forward, мобильное меню |
| UX-04 | Создать пост/статью где разрешено, медиа, мультицели, валидация до send, прогресс, preview labelled approximate, модерация, draft, retry только безопасный |
| UX-05 | Неделя/месяц/список, фильтры, создание на дату, перенос и conflict при гонке с executor; mobile/keyboard альтернативы drag-drop |
| UX-06 | История survives restart, mixed outcomes, empty/unknown, rejected/cancelled, pagination equal-time, поиск и фильтр, отсутствие утечки; SUBMITTED не DONE и без retry |
| UX-07 | Клиентская карточка, pending очередь, approve/reject reason, запрет чужих ID, recovery через существующий контракт |
| UX-08 | Подключение/переподключение, transient/confirmed_dead, stale credential, BANNED/CB без обхода, мастер доступен |
| UX-09 | Повторное использование медиа, шаблон → draft, safe links, cross-profile deny, retention и отсутствие удаления |
| UX-10 | Новый пользователь понимает создать/планировать/проверить; notification dedup/scope, без секретов, отказ/отписка где применимо |
| UX-11 | Установка PWA на поддерживаемом устройстве, offline network error, no private caches, logout, upload/keyboard/safe-area, fallback web |
| UX-12 | Регрессия всех включённых функций, mock E2E, CI и независимый QA; отдельные live smoke и backup/restore/deploy gates существующего ROADMAP |
| UX-13 (future) | Binding expiry/replay/revoke, foreign profile denied, repeated update/callback dedup, moderation/time/status parity with Web, private chats, no secrets; Mini App initData server validation if selected |

## E2E и визуальная проверка
Playwright Chromium в GitHub Actions уже устанавливается. Добавлять герметичные browser tests с синтетическими данными и адаптерами-моками; реальные публикации в CI запрещены. Viewports 360/390/768/1440 CSS px; проверять горизонтальный overflow, фокус, контраст, touch targets, загрузку/ошибки/keyboard/reduced-motion, обе темы. Реальные iOS Safari и Android Chrome — отдельный owner/device smoke; эмуляция Chromium не доказывает совместимость. Критические сценарии: создать/запланировать/редактировать/отменить, одобрить/отклонить, upload, mixed/SUBMITTED, отключённый канал, сеть/истёкшая сессия, изоляция. Сохранять sanitized скриншоты и тестовые отчёты; без реальных секретов.
CI: ruff, format, mypy, pytest на 3.11 и 3.13, doc links, новые route smoke. Не считать «тест не запущен» PASS. Независимый reviewer проверяет exact HEAD/diff и CI; один вердикт, re-review дефекта и affected contracts, class-of-defect coverage; после двух неудачных циклов эскалация координатору, не отмена обязательного QA. Существующий WORKFLOW имеет приоритет, в том числе squash/owner merge. Не вводить параллельный процесс ревью (см. также открытый PR #50).

## Целевые метрики (не измерены)
- Обычный пост из готовых текста/медиа: целевой median 30–60 секунд на заранее оговорённом сценарии без сетевого ожидания площадки; измерить baseline и после UX-04 на нескольких прогонах, не выдавать за гарантированное время.
- Основной редактор — один экран/маршрут без обязательного мастера.
- 0 потерянных подтверждённых черновиков в сценариях тестов.
- 100% проверенных комбинаций статусов различают подтверждённое и неподтверждённое; no duplicate operation.
- Пилот не ждёт P2, однако все P0, security/recovery/live platform gates обязательны. Реальное usability/device testing ещё не выполнено.

## Финальная проверка плана 2026-11-24
Reviewed main 58dedfcb: согласовано направление, исправлены противоречия T-212/T-209, moderation/idempotency/time/history, UX-12 не зависит от будущего бота. Открытые PR #32 (T-210), #33 (T-406), #36 (T-410), #41/#45/#42 (deploy/monitoring), #47 (recovery), #49 (T-401), #50 (QA process) не считаются merged и не дублируются. Документационный PR не утверждает выполненные тесты, новый UI или production-ready. Блокеры реализации: отдельное проектирование persistence/time migration, актуальная проверка security/ops dependencies, QA и owner/device smoke. Решение о ручном SUBMITTED retry, per-platform text, Mini App, политике retention и production rollout — отдельные согласования.
