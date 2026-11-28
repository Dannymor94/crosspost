"""Pilot must not send, including direct route calls."""

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from crosspost.web.app import create_app
from crosspost.web.live_gate import live_publication_enabled, require_live_publication


def test_live_publication_explicit_gate(monkeypatch):
    monkeypatch.delenv("CROSSPOST_LIVE_PUBLICATION_ENABLED", raising=False)
    monkeypatch.setenv("CROSSPOST_AUTH_MODE", "production")
    assert not live_publication_enabled()
    for value in ("false", "invalid", "TRUE", ""):
        monkeypatch.setenv("CROSSPOST_LIVE_PUBLICATION_ENABLED", value)
        assert not live_publication_enabled()
        with pytest.raises(HTTPException) as exc:
            require_live_publication()
        assert exc.value.status_code == 503
    monkeypatch.setenv("CROSSPOST_LIVE_PUBLICATION_ENABLED", "true")
    assert live_publication_enabled()


def test_all_direct_publication_routes_have_release_gate():
    expected = {
        "/api/profiles/{profile_id}/publish",
        "/api/profiles/{profile_id}/publish/{publication_id}/retry/{channel}",
        "/api/profiles/{profile_id}/scheduled/{scheduled_id}/publish-now",
    }
    guarded = {
        route.path
        for route in create_app().routes
        if isinstance(route, APIRoute)
        and "POST" in route.methods
        and any(dep.call is require_live_publication for dep in route.dependant.dependencies)
    }
    assert guarded == expected
