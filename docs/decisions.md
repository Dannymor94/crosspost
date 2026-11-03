# Architecture decisions

Short dated entries. One paragraph each.

---

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
