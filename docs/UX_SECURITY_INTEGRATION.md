# UX and security integration rehearsal

Sources: UX validation `b982e2e6fc08ef79ab4bdd90e55b15c8f6b49dc4` (#72), security `f1f406c2096c1c97a6ac6d3262ba0cd61978017d` (#82), actual main `29fcc94d52e295e3737ee39417380a94e6060d19`. This is not a delivery branch or authorization to deploy.

- Retain the combined UX scheduler, draft, operation, moderation, calendar and history paths.
- Use actual-main T304 parent (T406) and a no-DDL marker merging T304 UX validation with the T409 security chain. Extend existing disposable SQLite graph checks to security and prior UX heads.
- Include account registration/recovery/support, server admission limits, CSRF, generation revocation and owner MFA. Current UI attaches proof to unsafe fetch and XHR while preserving operation headers. Mock browser acceptance checks proof presence.
- Existing QA covers earlier source SHAs, not the combined tree. Exact CI, independent review, in-flight generation fences, all GET effects, sensitive settings, legacy client provisioning, new browser login/account acceptance, real HTTPS/deployment and live platform smoke remain release gates. No real user data used.
- PWA and actual self-service platform sign-in are separate tasks. Registration flag remains off by default.
