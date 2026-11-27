# Owner MFA — implementation contract (draft)

This PR contains only RFC 4226/6238 primitives and test vectors. It does NOT turn on
MFA, persist enrollment, enforce login MFA or provide recovery. No real secrets
or authenticator devices are used.

## Next implementation slice

- A dedicated per-owner row with a Fernet-encrypted 160-bit TOTP secret, unique
  owner ID FK, last accepted step and enrollment state. Keep encrypted pending
  enrollment short-lived; do not activate without password re-auth and a valid
  code. No raw secret in logs, URL query, storage, or public artifact.
- At activation, atomically invalidate old owner session generations. Enrolled
  owner login must require both password and TOTP or a one-use backup code before
  issuing a cookie. Unenrolled production owner must not get a normal privileged
  session; use an explicit local bootstrap path. Development compatibility must
  remain explicit; fail closed if vault/decryption fails.
- TOTP uses 30-second steps, six ASCII digits, ±1 skew. Match is only a candidate;
  SQL CAS must enforce last_step < matched_step, account incarnation and generation
  before issuance. Concurrent use of the same step has exactly one winner.
- Eight random 128-bit backup codes shown once, store only purpose-bound digests
  tied to owner/enrollment incarnation. Consume atomically, no replay. Never
  include raw codes in telemetry. CLI break-glass must require host access,
  explicit operator confirmation and audit; invalidate sessions and prevent a
  normal production login until re-enrolled. No client recovery bypass of MFA.
- Shared auth admission and CSRF apply to all MFA operations. Owner step-up
  needs MFA and password for invitation, recovery issuance and sensitive settings.
- Verify RFC vectors, expiry, races, wrong password/code, backup replay, vault
  failure, old sessions, lost device workflow and browser/HTTPS acceptance.
  The final database migration follows T409 auth_attempts. Independent QA and
  explicit owner enrollment are mandatory before enabling production onboarding.

## Boundaries

The primitive's last_step parameter is not a replay ledger. Without a persisted
CAS it is insufficient for production MFA. Keep new auth routes disabled until
the full enrollment/login/recovery state machine is implemented and reviewed.


## Implementation update

The next draft implements local terminal enrollment/replacement, encrypted user columns, atomic factor consumption and mandatory factor at login for enrolled owners; unenrolled production owners are rejected. Enrollment consumes the confirming step, advances the session generation and returns backup codes once. The existing `mfa_changed_at` is a last-change timestamp, not an append-only audit. Local re-enrollment requires host access, current password and explicit confirmation. Full fresh-factor step-up on sensitive routes, durable operator audit, newer UX integration and owner recovery drill are still pending. No production enrollment has occurred.
