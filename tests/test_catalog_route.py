"""The public model route uses one enriched source for both API formats."""

import httpx
import pytest

from openvidia import proxy_app
from openvidia.proxy_state import ProxyState, ProxyStats


@pytest.mark.asyncio
async def test_model_route_preserves_ids_and_enriches_both_client_formats(monkeypatch):
    constructor = httpx.AsyncClient
    upstream = constructor(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "vendor/current"},
                        {"id": "vendor/legacy", "validation": {"status": "passed"}},
                    ],
                    "models": [{"id": "obsolete/compatibility-copy"}],
                },
            )
        )
    )
    monkeypatch.setattr(proxy_app.httpx, "AsyncClient", lambda **_: upstream)

    async def enrich(_self, _client, models):
        return [
            {**model, **({"catalog": {"updated_at": "2026-11-01"}} if i == 0 else {})}
            for i, model in enumerate(models)
        ], {"stale": False, "matched_models": 1}

    monkeypatch.setattr(proxy_app.BuildCatalog, "enrich", enrich)
    app = proxy_app.create_app(ProxyState(["test-key"], ProxyStats(), lambda _: None))
    try:
        async with constructor(
            transport=httpx.ASGITransport(app), base_url="http://localhost:1919"
        ) as client:
            response = await client.get("/v1/models")
        assert response.status_code == 200
        body = response.json()
        expected = ["openvidia", "vendor/current", "vendor/legacy"]
        assert [row["id"] for row in body["data"]] == expected
        assert [row["id"] for row in body["models"]] == expected
        assert body["models"][1]["catalog"] == body["data"][1]["catalog"]
        assert "validation" not in body["data"][2]
        assert body["catalog"] == {"stale": False, "matched_models": 1}
    finally:
        await upstream.aclose()
