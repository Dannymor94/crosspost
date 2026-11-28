# Browser-channel onboarding: release boundary (draft)

## Current limitation

VK wall/channel and Yandex use a real headful browser on the machine running
Crosspost. The existing begin response does not stream or transfer that window
to a phone. Opening the login page on the client's own phone does not establish
the server browser's session. Never request the client's password, cookie export,
or login code in chat. Do not expose a server-wide VNC/CDP/debug endpoint.

## MVP boundary

The legacy browser/credential mutation endpoints are operator-only in production.
The new client initial connection routes are opt-in. Existing development tests
retain legacy access. Browser confirmation must stay on the exact HTTPS probe
host and cookie domains must match at a domain boundary; this is not yet a complete
proof of authenticated account or rights. Do not label browser channels as
self-service before actual device/provider acceptance.

## Candidate for real mobile onboarding

1. Inspect the official login UI of each provider on the actual deployment
   egress, using a consenting synthetic/test account. Record only redacted
   landmarks and capability (QR / device approval / browser interaction). Do
   not assume both providers support the same mechanism. Prefer provider-owned
   OAuth/device or QR authorization over collecting passwords in Crosspost.
2. If QR/device approval exists, use a short-lived isolated browser context with
   profile/account generation and opaque flow binding, one active flow per
   shared account group, global quota, expiry and cancellation. Only return
   provider-approved QR region or authorized link; do not expose full browser
   control, cookies, browser debugging, or arbitrary navigation. Prove supported
   HTTPS origins and QR content from live evidence. Document same-phone and
   second-device alternatives; do not promise same-phone support without proof.
3. If an actual interactive browser is necessary, require an independently
   reviewed isolated per-flow remote browser transport (not a shared desktop).
   Restrict egress, destinations, input, clipboard, uploads/downloads,
   screenshots/logging, resource quotas and lifetime. Bind every message to
   the authenticated flow and revoke on logout/expiry. Explicit approval before
   implementing this higher-risk option.
4. Verify account and publishing permission on the protected provider resource,
   then persist encrypted session atomically against the actor generation,
   profile incarnation, shared-session key and prior credential snapshot. No
   overwrite or retry of unknown result. Validate VK shared-channel semantics.
5. Acceptance: two clients/parallel tabs, logout during confirmation, expired QR,
   false positive cookies/redirects, provider denial, network timeout, phone
   installation, proxy no-fallback and no leakage. No real publication except
   separately authorized owner acceptance.

Until evidence is available, browser channels remain assisted and the UI must
say so. This is a gate, not a completed remote login implementation.
