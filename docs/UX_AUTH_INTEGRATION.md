# UX/auth integration — validation checkpoint (2026-11-27)

Validation only. No merge to main, deployment, live accounts or schema change.

## Verified inputs

- UX tip `71d18357a305fb85d88f0db5de6f94165415999b` (client help stacked on text reuse).
- T-406 #33 `e39c6a56bf7fcb1649a6c204bd9afbd1641095f8`: prior exact-head CI and independent technical PASS, open owner-only merge; session generation on recovery, not a complete auth release.
- T-410 #36 `ee503a8381d51f68d01b1ca9ece08383d7b49382`: reuse existing dynamic A/B/anonymous route matrix without changing original source PR. Extend nested checks for move, history and draft scope. This is author-run regression, not independent QA.
- Actual main `58dedfcb5226998190ddbce43b138eb378f655af`.

## Integration boundary

T-406 and UX-01 independently descend from `t203_fencing`. T-406 has `t406_session_generation`; UX-01 chain ends at `t218_editor_draft`. Both are currently unmerged. Do not deploy two heads or silently reparent a revision that may already exist in a database. Choose final migration graph after owner delivery-order decision; verify fresh DB, main upgrade, recovery/restore, one Alembic head and actual retained data. A merge revision preserving both existing IDs is a candidate, not an approved policy.

Eight overlaps: CHANGELOG, BACKLOG, STATUS, decisions, models.py, test_migrations.py, test_breaker_dispatch.py, test_app_routes_smoke.py. T-406 auth/session and user repository files otherwise disjoint from UX tip. Reconcile all source changes without losing route tests or migration evidence.

T-406 API-only recovery: admin password confirmation, purpose-bound 15-minute token, client sets new password, revokes prior sessions. UI must not expose token in URL/log/storage; owner-assisted transfer requires a safe agreed channel. Public registration is not yet a delivered feature. T-407–410 (rate limit, MFA, CSRF, secure cookies, tenant checks) and session-incarnation handling remain release gates. Do not duplicate existing T-406 implementation.

T-405 retention policy blocks durable media library/templates; text reuse and client guidance are isolated preparation only.

Next: validate this matrix on the UX tree, reconcile delivery ancestry, obtain owner delivery order, integrate and test auth on a separate candidate, then add UI under exact approved contract. No production go-ahead.
