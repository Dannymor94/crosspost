# Telegram — operator runbook

## Connection

- **API tier** (Telethon / MTProto).
- Credential stored in vault as JSON: `{"api_id": "...", "api_hash": "...", "session": "<StringSession>"}`.
- Login: interactive wizard — phone → code → 2FA (if set). Session persists in vault; no re-login unless session is revoked.
- `peer_access_hash` for channels/groups is resolved at target-creation time via `get_entity()` and stored in `channel_targets.peer_access_hash`. It is **not accepted from the client API** — only set by discovery.

## Targets

Each Telegram posting destination is stored as a row in `channel_targets`:

| column | meaning |
|---|---|
| `value` (encrypted) | channel username (`@channel`) or numeric ID (`-100...`) |
| `topic_id` | forum topic ID, or `NULL` for the general timeline |
| `topic_title` | cached topic name (set at target-creation, never re-fetched at list time) |
| `peer_access_hash` | Telethon access hash (set by discovery, never by client) |

**General topic rule**: topic ID `1` in Telegram means "General". On creation `topic_id = 1` is normalised to `NULL` (general timeline, no `reply_to` needed).

**Message-ID vs topic-ID**: `t.me/channel/123` looks like a topic link but `123` can be a plain message ID. Resolution: call `list_forum_topics()` and check whether `123` appears in the list. If not (or if the channel is not a forum), `topic_id` is set to `NULL`.

## Publishing to a forum topic

```python
await client.send_message(entity, text, reply_to=topic_id)   # topic_id is an int
await client.send_file(entity, file, caption=text, reply_to=topic_id, supports_streaming=True)
```

Use plain `int`, **not** `InputReplyToMessage`. `supports_streaming=True` for video.

When `topic_id` is `NULL`, omit `reply_to` entirely (posts to general timeline).

## Target creation

Two flows:

### Picker (`POST .../channels/telegram/dialogs`)

1. `GET .../channels/telegram/dialogs` — lists all postable chats (TTL 10 min).
2. `GET .../channels/telegram/dialogs/{target_value}/topics` — lists forum topics for one chat (TTL 10 min).
3. `POST .../channels/telegram/targets/bulk` — operator selects chats/topics from picker, bulk-creates targets. Stores `topic_title` from in-memory topic cache.

### Paste link (`POST .../channels/telegram/targets/from-link`)

Body: `{"url": "https://t.me/..."}`. Flow:

1. `parse_telegram_link(url)` → `(target_value, raw_topic_id)` — regex, no network.
2. `get_entity(target_value)` — resolves chat, stores `access_hash`.
3. If `raw_topic_id` is set: `list_forum_topics()` → find match → check `can_post` → get `topic_title`. If topic not found → `topic_id = NULL` (was a message ID).
4. Creates target idempotently (returns `created: false` if already exists, backfills `access_hash`/`topic_title` if missing).

## API endpoints

| method | path | notes |
|---|---|---|
| `GET` | `.../channels/telegram/dialogs` | lists postable chats; `?refresh=true` bypasses TTL |
| `GET` | `.../channels/telegram/dialogs/{target_value}/topics` | lists forum topics |
| `POST` | `.../channels/telegram/targets/bulk` | bulk-create from picker selection |
| `POST` | `.../channels/telegram/targets/from-link` | add single target by t.me URL |

All discovery endpoints are admin-only.

## Testing

All tests use mocked Telethon clients — no live credentials needed.

Key mocks: `get_entity`, `list_forum_topics`, `list_postable_dialogs`.
Idempotency is tested by calling `from-link` twice with the same URL and checking `created: false`.
