# Instagram Reels — operator runbook

## Connection

- **API tier** (graph.instagram.com, NOT browser).
- Requires a **Professional account** (Creator or Business).
- Meta developer app must be in **Development mode**; the Instagram account must be added as an **Instagram tester** in the app dashboard.
- Required permission: `instagram_business_content_publish`.
- Obtain `user_id` from `GET /v26.0/me?fields=user_id,username`. Use that ID for all API calls. The `id` field in the same response is the **app-scoped ID** — do not use it for publishing.
- Credential stored in vault as JSON: `{"access_token": "...", "ig_user_id": "..."}`.
- Token lifetime: ~60 days. Refresh is not automated; the wizard prompts for a new token when the current one is near expiry.

## Publishing flow

1. `POST /{user_id}/media` with `media_type=REELS`, `video_url=<signed URL>`, `caption`.
   - `video_url` must be publicly reachable by Meta. It is a signed `/public-media/{token}` link (see *Signed media links*).
   - Resumable upload (`upload_type=resumable`, rupload.facebook.com) is **not available** for Instagram Login tokens — confirmed by live API probe 2026-10-28.
2. Poll `GET /{container_id}?fields=status_code,status` every `INSTAGRAM_POLL_SECONDS` (default 30 s) until `status_code` is `FINISHED` or a terminal state. Maximum wait governed by `INSTAGRAM_POLL_TIMEOUT_SECONDS`.
3. On `FINISHED`: `POST /{user_id}/media_publish` with `creation_id={container_id}` → returns `media_id` (stored as `external_id`).
4. On `ERROR` or `EXPIRED`: log `status` text verbatim, mark task `failed`. The container ID checkpoint is cleared so the next retry creates a fresh container.
5. On timeout (Meta processing too slow): task is marked `failed` with a message; the checkpoint is kept so the next retry resumes from the existing container.

## Checkpoint / idempotency

- Container ID is saved to the idempotency store immediately after creation, before publish.
- If the process restarts between step 1 and step 3, the next attempt resumes from the existing container rather than creating a duplicate.
- `publication_id` is the primary deduplication key (generated before any API call). `media_id` is the receipt, not the dedup key.

## Signed media links

Media is served via `/public-media/{token}`. The token is an HMAC-SHA256-signed base64url blob containing the relative path and expiry timestamp.

Required env vars:
- `MEDIA_URL_SECRET` — signing key (generate: `python -c "import secrets; print(secrets.token_hex(32))"`).
- `MEDIA_URL_TTL_SECONDS` — token lifetime in seconds (default 3600; use 7200+ for slow connections).
- `INSTAGRAM_PUBLIC_BASE_URL` — the publicly reachable base URL, e.g. `https://my-server.com`.

The route accepts both `GET` and `HEAD`. Meta's video ingestor sends `HEAD` before `GET`; both return the same headers (`Content-Type`, `Content-Length`, `Accept-Ranges`). The full token is never written to access logs (redacted to first 8 chars + `…`).

## Network requirements

- **Meta must reach `INSTAGRAM_PUBLIC_BASE_URL` publicly.** On a production VPS this is the server's own domain. For local development use the SSH tunnel (`make tunnel` / `make tunnel-stop`).
- **Instagram API calls must leave from a non-Russian IP.** Configure `INSTAGRAM_PROXY_URL` (SOCKS5/HTTP proxy). On a VPS outside Russia no proxy is needed.
- On macOS with Happ VPN, terminal/Python traffic does **not** go through the VPN — egress stays Russian. Verify with `curl -s ifconfig.me` before publishing.

### Local development tunnel

`INSTAGRAM_TUNNEL_AUTO=true` (default when `INSTAGRAM_PUBLIC_BASE_URL` is not set) starts a localhost.run SSH tunnel automatically before each container creation. The current tunnel URL is kept in `tunnel.state` in-process (falls back to `INSTAGRAM_PUBLIC_BASE_URL` env var).

```
make tunnel       # start tunnel, print URL
make tunnel-stop  # kill tunnel
```

`GET /tunnel-health` returns a per-process nonce for confirming the tunnel reached the right server process.

## Error reference

| Code / status | Meaning | Action |
|---|---|---|
| 190 (OAuthException) | Invalid/expired token | Re-issue token, re-run wizard |
| 200 (OAuthException) "API access blocked" | Meta-side block on the token or app | Issue a fresh token with all required permissions; confirm `instagram_business_content_publish` is granted |
| 10 | Permission denied | Add `instagram_business_content_publish` to the token |
| `status_code=ERROR` | Meta processing error | Log `status` text verbatim; check video codec/resolution (H.264 + AAC, MP4/MOV); retry |
| `status_code=EXPIRED` | Container expired before publish | Create a new container (checkpoint is cleared automatically) |
| Proxy error | `INSTAGRAM_PROXY_URL` unreachable | Check VPN/proxy client is running |

## Troubleshooting log

- **2026-10-28** — The original token returned `OAuthException code 200 "API access blocked"` from both Russian and non-Russian IPs. A new token issued with `instagram_business_content_publish` permission returned HTTP 200. Block was at the token/permission level, not geographic.
- **2026-10-28** — First successful Reel published end-to-end from a GitHub Actions runner (ubuntu-latest). Meta fetched the video through a localhost.run tunnel running on the developer's Mac. Container reached `FINISHED` in ~30 s; `media_id` returned.
- **2026-10-28** — `HEAD /public-media/{token}` returned 405. Fixed: route changed to `@router.api_route(..., methods=["GET", "HEAD"])`.
- Happ VPN on macOS does **not** route terminal or Python traffic. Local egress remains Russian even with VPN active.

## Open items

- [ ] Instagram token auto-refresh — not implemented. Token expires in ~60 days; renew via wizard.
- [ ] `plan import` currently skips the entire row when a video exceeds one channel's size limit. Should drop only the over-limit channel and publish to the rest.
- [ ] Wizard copy — check for any remaining "Facebook Login" wording that should say "Instagram Login".
