# Changelog

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
