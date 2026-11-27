# UX/auth merge rehearsal — 2026-11-27

Validation only; never merge this PR as a delivery replacement or deploy it. Owner order/graph decision remains open.

Inputs: UX/T-410 validation #71 `bced1ef484a8d338822b08170fa221e5fee129a1` (CI success); T-406 #33 `e39c6a56bf7fcb1649a6c204bd9afbd1641095f8` (prior independent technical PASS); main `58dedfcb5226998190ddbce43b138eb378f655af`. Both source branches retained.

Candidate: preserve both migration histories and add a no-DDL merge revision `t406_ux_merge` with both parents. Reconcile all eight overlapping files; preserve T-406's source, tests and docs, UX route smoke and existing draft/history/operation changes. The test suite checks one head, upgrades from main and either branch, restart, schema parity, and retention of user generation, publication receipt and existing draft. Fresh/legacy DB and API/tenant regression use existing CI. Downgrading the merge marker alone is not a production rollback plan: old auth downgrade can discard session generations and UX downgrades can refuse existing state. No production downgrade, stamp or backup restore is authorized.

Auth recovery is still API-only; registration, support UI, rate limits, secure cookie/CSRF, admin MFA, logout/session-incarnation fencing, and delivery QA are separate. Do not expose recovery tokens in URLs/log/storage. The T-304 migration is not part of this candidate; it must be reconciled in the final graph. Retention T-405 remains an owner decision.

CI pending on this candidate; non-author QA and owner approval required for delivery.
