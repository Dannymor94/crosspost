"""Smoke test: create_app() registers every route called by index.html.

Catches missing router includes (like the telegram_discovery router incident)
without requiring a running server.
"""

from __future__ import annotations

import re

import pytest

from crosspost.web.app import create_app


def _norm(path: str) -> str:
    """Collapse all {param} / {param:type} to {*} so parametric paths compare equal."""
    return re.sub(r"\{[^}]+\}", "{*}", path)


@pytest.fixture(scope="module")
def _route_keys():
    """Build (METHOD, normalized_path) set from the real app."""
    app = create_app()
    keys: set[tuple[str, str]] = set()
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for m in route.methods:
                keys.add((m, _norm(route.path)))
    return keys


# Every (method, path) pair that index.html calls.
# Path params collapsed to {*} to match FastAPI's registered paths after _norm.
_EXPECTED: list[tuple[str, str]] = [
    ("GET", "/health"),
    ("GET", "/health/live"),
    ("GET", "/pwa/config"),
    ("GET", "/pwa/manifest.webmanifest"),
    ("GET", "/pwa/sw.js"),
    # auth
    ("GET", "/account"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/users/support/{*}/inspect"),
    ("GET", "/api/users/invitations"),
    ("POST", "/api/users/invitations"),
    ("POST", "/api/users/invitations/{*}/renew"),
    ("POST", "/api/users/invitations/{*}/revoke"),
    ("GET", "/api/auth/csrf"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/recover"),  # T-406: owner-issued token redemption
    # profiles
    ("GET", "/api/profiles"),
    ("POST", "/api/profiles"),
    # settings
    ("GET", "/api/settings/browser"),
    ("PUT", "/api/settings/browser"),
    # users
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("POST", "/api/users/{*}/recovery-token"),  # T-406: admin-only issuance
    # channels (per-profile)
    ("GET", "/api/profiles/{*}/client-connection/capabilities"),
    ("POST", "/api/profiles/{*}/client-telegram/begin"),
    ("POST", "/api/profiles/{*}/client-telegram/{*}"),
    ("POST", "/api/profiles/{*}/client-connection/{*}"),
    ("GET", "/api/profiles/{*}/channels"),
    ("POST", "/api/profiles/{*}/channels/{*}/validate"),
    ("POST", "/api/profiles/{*}/channels/{*}/disconnect"),
    ("POST", "/api/profiles/{*}/channels/{*}/connect"),
    ("PUT", "/api/profiles/{*}/channels/{*}/target"),
    # browser-login wizard
    ("POST", "/api/profiles/{*}/channels/{*}/browser-login/begin"),
    ("POST", "/api/profiles/{*}/channels/{*}/browser-login/cancel"),
    ("POST", "/api/profiles/{*}/channels/{*}/browser-login/confirm"),
    # Telegram login
    ("POST", "/api/profiles/{*}/channels/telegram/login/begin"),
    ("POST", "/api/profiles/{*}/channels/telegram/login/cancel"),
    ("POST", "/api/profiles/{*}/channels/telegram/login/send-code"),
    ("POST", "/api/profiles/{*}/channels/telegram/login/sign-in"),
    ("POST", "/api/profiles/{*}/channels/telegram/login/import-session"),
    # Telegraph
    ("POST", "/api/profiles/{*}/channels/telegraph/create-account"),
    # multi-targets
    ("GET", "/api/profiles/{*}/channels/{*}/targets"),
    ("POST", "/api/profiles/{*}/channels/{*}/targets"),
    ("PATCH", "/api/profiles/{*}/channels/{*}/targets/{*}"),
    ("DELETE", "/api/profiles/{*}/channels/{*}/targets/{*}"),
    ("POST", "/api/profiles/{*}/channels/telegram/targets/from-link"),
    ("POST", "/api/profiles/{*}/channels/telegram/targets/bulk"),
    # editor draft
    ("GET", "/api/profiles/{*}/editor-draft"),
    ("PUT", "/api/profiles/{*}/editor-draft"),
    # UX-01 operation recovery and scheduling settings
    ("GET", "/api/profiles/{*}/operation-status"),
    ("GET", "/api/profiles/{*}/settings/timezone"),
    ("PUT", "/api/profiles/{*}/settings/timezone"),
    ("POST", "/api/profiles/{*}/scheduled/resolve-time"),
    ("GET", "/api/profiles/{*}/settings/moderation"),
    ("PUT", "/api/profiles/{*}/settings/moderation"),
    # publish
    ("GET", "/api/profiles/{*}/publish/targets"),
    ("POST", "/api/profiles/{*}/publish/{*}/retry/{*}"),
    # scheduler
    ("GET", "/api/profiles/{*}/scheduled"),
    ("GET", "/api/profiles/{*}/scheduled/pending"),
    ("GET", "/api/profiles/{*}/scheduled/report"),
    ("GET", "/api/profiles/{*}/history"),
    ("POST", "/api/profiles/{*}/history/search"),
    ("POST", "/api/profiles/{*}/scheduled/{*}/move"),
    ("GET", "/api/profiles/{*}/scheduled/{*}"),
    ("DELETE", "/api/profiles/{*}/scheduled/{*}"),
    ("POST", "/api/profiles/{*}/scheduled/{*}/publish-now"),
    ("POST", "/api/profiles/{*}/scheduled/{*}/approve"),
    ("POST", "/api/profiles/{*}/scheduled/{*}/reject"),
    ("GET", "/api/profiles/{*}/scheduled/{*}/screenshots"),
    # import
    ("GET", "/api/profiles/{*}/import/settings"),
    ("PUT", "/api/profiles/{*}/import/settings"),
    # admin logs
    ("GET", "/api/admin/logs"),
    ("GET", "/api/admin/logs/summary"),
]


@pytest.mark.parametrize("method,path", _EXPECTED, ids=[f"{m} {p}" for m, p in _EXPECTED])
def test_route_registered(method, path, _route_keys):
    assert (method, path) in _route_keys, (
        f"{method} {path} is used by index.html but not registered in create_app()"
    )
