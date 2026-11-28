"""PWA delivery, disabled-by-default contract and offline cache boundaries."""

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from crosspost.web.app import create_app

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"


async def test_pwa_routes_opt_in_and_public_cache_headers(monkeypatch):
    monkeypatch.delenv("CROSSPOST_PWA_ENABLED", raising=False)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        assert (await c.get("/pwa/config")).json() == {"enabled": False}
        assert (await c.get("/pwa/sw.js")).status_code == 404
        assert (await c.get("/pwa/manifest.webmanifest")).status_code == 404
        monkeypatch.setenv("CROSSPOST_PWA_ENABLED", "true")
        assert (await c.get("/pwa/config")).json() == {"enabled": True}
        worker = await c.get("/pwa/sw.js")
        assert worker.status_code == 200
        assert worker.headers["service-worker-allowed"] == "/"
        assert worker.headers["cache-control"] == "no-store"
        manifest = await c.get("/pwa/manifest.webmanifest")
        assert manifest.status_code == 200
        assert manifest.json()["start_url"] == "/"
        assert manifest.headers["cache-control"] == "no-store"
        for path in ("/", "/client", "/account"):
            assert (await c.get(path)).headers["cache-control"] == "no-store"


def test_public_manifest_and_worker_are_bounded():
    manifest = json.loads((FRONTEND / "pwa/manifest.webmanifest").read_text())
    for item in manifest["icons"]:
        png = (FRONTEND / item["src"].removeprefix("/static/")).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        size = int(item["sizes"].split("x")[0])
        assert int.from_bytes(png[16:20], "big") == size
        assert int.from_bytes(png[20:24], "big") == size
    worker = (FRONTEND / "pwa/sw.js").read_text()
    assert 'request.method !== "GET"' in worker
    assert 'request.mode !== "navigate"' in worker
    assert '["/", "/client"].includes(url.pathname)' in worker
    assert worker.count("cache.put(") == 1
    assert "cache.put(OFFLINE, response)" in worker
