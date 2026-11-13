"""Official Build metadata enriches the API intersection without inventing IDs."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from openvidia.catalog import SOURCE, BuildCatalog, enrich_models, parse_build_metadata

FIXTURE = Path(__file__).parent / "fixtures/nvidia-build-latest.html"
MISTRAL = "mistralai/mistral-large-4-1t-a52b"
KIMI = "moonshotai/kimi-k3_1-pro"


def test_parse_real_next_flight_resource_schema():
    metadata = parse_build_metadata(FIXTURE.read_text())
    assert metadata[MISTRAL]["display_name"] == "Mistral Large 4"
    assert metadata[MISTRAL]["updated_at"].startswith("2026-10-29")
    assert "Tool Use" in metadata[MISTRAL]["use_cases"]
    assert metadata[MISTRAL]["endpoint_kind"] == "free_chat"
    assert metadata[MISTRAL]["source"] == SOURCE
    assert "black-forest-labs/flux-3-image-klein" not in metadata
    assert "nvidia/nemotron-3_5-embed-1b" not in metadata


@pytest.mark.asyncio
async def test_build_preview_never_creates_a_selectable_api_model(tmp_path):
    cache = BuildCatalog(tmp_path / "cache.json")
    models = [{"id": MISTRAL}, {"id": "old/legacy-chat"}]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=FIXTURE.read_text()))
    ) as client:
        enriched, info = await cache.enrich(client, models)
    assert [model["id"] for model in enriched] == [MISTRAL, "old/legacy-chat"]
    assert all(model["id"] != KIMI for model in enriched)
    assert enriched[0]["catalog"]["display_name"] == "Mistral Large 4"
    assert "catalog" not in enriched[1]
    assert "catalog" not in models[0]
    assert info["scope"] == "latest_page"
    assert info["matched_models"] == 1
    assert info["stale"] is False


@pytest.mark.asyncio
async def test_fetch_strips_inference_credentials_and_cookies(tmp_path):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.url.host == "build.nvidia.com"
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, text=FIXTURE.read_text())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        auth=("secret", "secret"),
        headers={"Authorization": "Bearer NVIDIA_SECRET", "X-Api-Key": "SECRET"},
        cookies={"session": "PRIVATE"},
    ) as client:
        cache = BuildCatalog(tmp_path / "cache.json")
        await cache.enrich(client, [{"id": MISTRAL}])
        await cache.enrich(client, [{"id": MISTRAL}])
    assert len(seen) == 2  # listing + unmatched detail; second refresh uses the hourly cache


@pytest.mark.asyncio
async def test_schema_change_preserves_stale_last_good_metadata(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "source": SOURCE,
                "refreshed_at": time.time() - 7200,
                "models": parse_build_metadata(FIXTURE.read_text()),
            }
        )
    )
    cache = BuildCatalog(path)
    original = path.read_text()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, text="<html>New unsupported schema</html>")
        )
    ) as client:
        enriched, info = await cache.enrich(client, [{"id": MISTRAL}])
    assert enriched[0]["catalog"]["display_name"] == "Mistral Large 4"
    assert info["stale"] is True
    assert path.read_text() == original


@pytest.mark.asyncio
async def test_failure_does_not_hide_existing_api_models(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(503))
    ) as client:
        enriched, info = await BuildCatalog(tmp_path / "cache.json").enrich(
            client, [{"id": "old/chat"}]
        )
    assert enriched == [{"id": "old/chat"}]
    assert info["stale"] is True
    assert info["refreshed_at"] is None


@pytest.mark.asyncio
async def test_default_cache_is_scoped_to_client_and_config(isolated_config_dir):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, text=FIXTURE.read_text())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as first:
        await enrich_models(first, [{"id": MISTRAL}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as second:
        enriched, _info = await enrich_models(second, [{"id": MISTRAL}])
    assert len(seen) == 2  # listing + detail; second client loads disk, no shared async lock
    assert "catalog" in enriched[0]


@pytest.mark.parametrize(
    "html", ["", "<html>not flight</html>", '<script>self.__next_f.push([1,"1:{}\\n"])</script>']
)
def test_unknown_schema_is_failure_not_empty_success(html):
    with pytest.raises(ValueError):
        parse_build_metadata(html)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"source": SOURCE, "refreshed_at": "NaN", "models": {}},
        {"source": SOURCE, "refreshed_at": 10**100, "models": {}},
    ],
)
def test_corrupt_or_nonfinite_disk_cache_is_ignored(tmp_path, payload):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(payload))
    cache = BuildCatalog(path)
    assert cache.refreshed_at == 0
    assert cache.models == {}


def test_detail_schema_returns_canonical_request_model_without_slug_guessing():
    from openvidia.catalog import parse_detail_api_ids

    html = (FIXTURE.parent / "nvidia-build-glm-detail.html").read_text()
    assert parse_detail_api_ids(html) == {"z-ai/glm-5.3-flash"}
    assert parse_detail_api_ids(html.replace("integrate.api.nvidia.com", "other.example")) == set()


@pytest.mark.asyncio
async def test_alias_is_only_resolved_from_official_request_schema_and_api_intersection(tmp_path):
    canonical = "z-ai/glm-5.3-flash"
    visited = []

    def respond(request):
        visited.append(str(request.url))
        name = (
            "nvidia-build-glm-list.html"
            if request.url.path == "/models"
            else "nvidia-build-glm-detail.html"
        )
        return httpx.Response(200, text=(FIXTURE.parent / name).read_text())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cache = BuildCatalog(tmp_path / "cache.json")
        enriched, info = await cache.enrich(client, [{"id": canonical}, {"id": "old/chat"}])
        await cache.enrich(client, [{"id": canonical}])
    assert enriched[0]["catalog"]["display_name"] == "glm-5-3-flash"
    assert (
        enriched[0]["catalog"]["api_model_source"] == "https://build.nvidia.com/z-ai/glm-5-3-flash"
    )
    assert [item["id"] for item in enriched] == [canonical, "old/chat"]
    assert info["matched_models"] == 1
    assert visited == [SOURCE, "https://build.nvidia.com/z-ai/glm-5-3-flash"]


@pytest.mark.asyncio
async def test_detail_schema_cannot_create_an_id_absent_from_live_api(tmp_path):
    def respond(request):
        name = (
            "nvidia-build-glm-list.html"
            if request.url.path == "/models"
            else "nvidia-build-glm-detail.html"
        )
        return httpx.Response(200, text=(FIXTURE.parent / name).read_text())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        enriched, info = await BuildCatalog(tmp_path / "cache.json").enrich(
            client, [{"id": "old/chat"}]
        )
    assert enriched == [{"id": "old/chat"}]
    assert info["matched_models"] == 0
