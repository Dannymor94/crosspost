# SETUP.md — ключи и доступы

Что нужно получить, чтобы система реально постила. Делай это **параллельно** работе агента:
код пишется и тестируется на моках, ключи нужны только для `make smoke`.

Порядок столбца «Когда»: 🟢 для MVP-0, 🟡 для остального API-тира, ⚪ post-MVP.

---

## Сводный список ключей

| Канал | Что получить | Где | Интерактив? | Когда |
|---|---|---|---|---|
| **Telegram** | `api_id`, `api_hash` | https://my.telegram.org → API development tools | да — разовый вход по телефону при первом запуске Telethon | 🟢 MVP-0 |
| **Telegram** | права админа бота-аккаунта в целевом канале + `@username`/id канала | сам канал | — | 🟢 MVP-0 |
| **ВКонтакте** | `access_token` сообщества (права `wall`, `photos`) + `group_id` | https://vk.com/apps?act=manage → своё приложение/сообщество, либо токен сообщества в настройках группы | нет | 🟢 MVP-0 |
| **Telegraph** | `access_token` | https://api.telegra.ph/createAccount (один GET-запрос) | нет | 🟡 |
| **YouTube** | OAuth client (`client_secret.json`) + разовая авторизация | https://console.cloud.google.com → включить YouTube Data API v3 → OAuth credentials | да — разовый OAuth в браузере | 🟡 |

---

## Детали по каналам

### Telegram (🟢 нужен для MVP-0)
1. Зайти на **my.telegram.org**, войти по номеру.
2. **API development tools** → создать приложение → забрать `api_id` и `api_hash`.
3. При первом запуске Telethon попросит код из Telegram — это разовый интерактив, дальше живёт `StringSession`.
4. Аккаунт, под которым логинится Telethon, должен быть **админом целевого канала** с правом постинга.
5. В `runtime/.env`: `TG_API_ID`, `TG_API_HASH`, `TG_TARGET_CHANNEL` (напр. `@my_channel`).

> Используем userbot (Telethon), а не Bot API — из-за транспорта в РФ. Пост уходит от лица канала.

### ВКонтакте (🟢 нужен для MVP-0)
1. Нужен **токен сообщества** с правами `wall` и `photos` (постинг от имени группы).
2. Проще всего — в управлении сообществом: **Настройки → Работа с API → Ключи доступа → создать ключ**.
3. Узнать `group_id` сообщества.
4. В `runtime/.env`: `VK_ACCESS_TOKEN`, `VK_GROUP_ID`.

### Telegraph (🟡)
1. Один запрос: `GET https://api.telegra.ph/createAccount?short_name=crosspost&author_name=...`
2. Из ответа забрать `access_token`.
3. В `runtime/.env`: `TELEGRAPH_ACCESS_TOKEN`.

### YouTube (🟡)
1. **console.cloud.google.com** → новый проект → включить **YouTube Data API v3**.
2. Создать **OAuth client (Desktop)** → скачать `client_secret.json` в `runtime/sessions/`.
3. Первая авторизация — разовый OAuth в браузере, токен сохранится в `YOUTUBE_TOKEN_PATH`.
4. Учесть суточную квоту (заливка видео дорогая по квоте).
5. В `runtime/.env`: `YOUTUBE_CLIENT_SECRET_PATH`, `YOUTUBE_TOKEN_PATH`.

---

## Браузерный тир (⚪ post-MVP) — ключей НЕ требует

WhatsApp, Instagram, Дзен, Яндекс — **не API-ключи, а живые сессии**: разовый вход (QR/логин) в персистентный профиль браузера. Получаются позже, в эпике 5–6, через флоу релогина. Для MVP игнорировать.

---

## Безопасность ключей

- Все значения — только в `runtime/.env` (он в `.gitignore`).
- **Ключ шифрования vault — НЕ в `runtime/.env`**, инжектится из окружения/keyring (см. PROJECT_STRUCTURE).
- Перед первым коммитом: `git status` не должен показывать `.env`, `runtime/`, `*.session`, `client_secret.json`.
- Утёкший ключ из истории гита = скомпрометирован: ротировать, не «удалять файл».

---

## Минимум для первого работающего поста (MVP-0)

Только это, остальное потом:
```
TG_API_ID=...
TG_API_HASH=...
TG_TARGET_CHANNEL=@...
VK_ACCESS_TOKEN=...
VK_GROUP_ID=...
```
