"""A slow model must not make the entire host and its catalog disappear."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from openvidia import config, proxy_app
from openvidia.proxy_state import ProxyState, ProxyStats
from openvidia.upstream_router import EndpointRouter

ENDPOINT = "https://configured.example/v1/"


@asynccontextmanager
async def client_for(monkeypatch, handler, *, blacklisted=False):
    class NoBuildMetadata:
        async def enrich(self, client, models):
            return models, {"scope": "latest_page", "stale": True}

    router = EndpointRouter([ENDPOINT])
    if blacklisted:
        router.mark_failure(ENDPOINT + "chat/completions")
    state = ProxyState(["nvapi-test"], ProxyStats(), lambda _line: None)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    actual_client = httpx.AsyncClient
    with monkeypatch.context() as patch:
        patch.setattr(proxy_app.httpx, "AsyncClient", lambda **_kwargs: upstream)
        patch.setattr(proxy_app, "EndpointRouter", lambda _endpoints: router)
        patch.setattr(proxy_app, "BuildCatalog", NoBuildMetadata)
        patch.setattr(config, "upstream_endpoints", lambda: [ENDPOINT])
        app = proxy_app.create_app(state)
    try:
        async with actual_client(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:1919"
        ) as client:
            yield client, router, state
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_model_read_timeout_preserves_shared_host_and_catalog(monkeypatch):
    seen = []

    def respond(request):
        seen.append((request.url.host, request.url.path))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "vendor/fast"}]})
        if json.loads(request.content)["model"] == "vendor/slow":
            raise httpx.ReadTimeout("model did not finish thinking", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with client_for(monkeypatch, respond) as (client, router, state):
        slow = await client.post(
            "/v1/chat/completions",
            json={"model": "vendor/slow", "messages": [{"role": "user", "content": "probe"}]},
        )
        assert slow.status_code == 504
        assert router.healthy_endpoints() == [ENDPOINT]
        catalog = await client.get("/v1/models")
        assert catalog.status_code == 200
        assert "vendor/fast" in [item["id"] for item in catalog.json()["data"]]
        fast = await client.post(
            "/v1/chat/completions",
            json={"model": "vendor/fast", "messages": [{"role": "user", "content": "probe"}]},
        )
        assert fast.status_code == 200
        assert state.model_health["vendor/slow"].too_slow > 0
    assert {host for host, _path in seen} == {"configured.example"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/models", "/v1/chat/completions"])
async def test_all_blacklisted_endpoints_still_probe_configured_host(monkeypatch, path):
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "vendor/fast"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with client_for(monkeypatch, respond, blacklisted=True) as (client, router, _state):
        assert router.healthy_endpoints() == []
        response = (
            await client.get(path)
            if path.endswith("models")
            else await client.post(
                path,
                json={"model": "vendor/fast", "messages": [{"role": "user", "content": "probe"}]},
            )
        )
        assert response.status_code == 200
        assert router.healthy_endpoints() == [ENDPOINT]
    assert len(seen) == 1
    assert seen[0].startswith(ENDPOINT)
