"""Optional NVIDIA Build metadata for models already in the live API catalog.

Build's newest page is richer than /v1/models, but a Free Endpoint badge does
not prove API access (some playground previews have no public API). Never add
or invent selectable model IDs from it: only annotate exact API intersections.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import weakref
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import config

SOURCE = "https://build.nvidia.com/models"
TTL_SECONDS = 3600
MAX_HTML_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,80}/[a-zA-Z0-9_.-]{1,160}$")
_FLIGHT = re.compile(r"self\.__next_f\.push\(")


def _resource_metadata(resource: dict) -> tuple[str, dict] | None:
    if resource.get("resourceType") != "ENDPOINT":
        return None
    raw_labels = resource.get("labels")
    if not isinstance(raw_labels, list):
        return None
    labels = {
        item["key"]: [value for value in item.get("values", []) if isinstance(value, str)]
        for item in raw_labels
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and isinstance(item.get("values"), list)
    }
    publishers = labels.get("publisher", [])
    name = resource.get("name")
    if len(publishers) != 1 or not isinstance(name, str):
        return None
    model_id = f"{publishers[0]}/{name}"
    if not _ID.fullmatch(model_id):
        return None
    tags = list(dict.fromkeys(labels.get("general", []) + labels.get("usecase", [])))[:24]
    normalized = {value.casefold() for value in tags}
    free = "Free Endpoint" in labels.get("nimType", [])
    chat = "chat" in labels.get("playgroundType", []) or bool(
        normalized & {"chat", "text-to-text", "tool use", "agentic coding"}
    )
    if not free or not chat:
        return None
    updated = resource.get("dateModified")
    if not isinstance(updated, str):
        return None
    try:
        parsed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        updated = parsed.astimezone(UTC).isoformat()
    except ValueError:
        return None
    display = resource.get("displayName")
    return model_id, {
        "updated_at": updated,
        "use_cases": [tag[:100] for tag in tags],
        "endpoint_kind": "free_chat",
        "source": SOURCE,
        "display_name": display[:200] if isinstance(display, str) else model_id,
    }


def _flight_records(html: str) -> list:
    """Decode JSON data from Next Flight without evaluating any JavaScript."""
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("catalog page exceeds size limit")
    decoder = json.JSONDecoder()
    fragments = []
    for index, match in enumerate(_FLIGHT.finditer(html)):
        if index >= 200:
            raise ValueError("too many catalog records")
        try:
            item, _end = decoder.raw_decode(html[match.end() :])
        except (ValueError, RecursionError):
            continue
        if isinstance(item, list) and len(item) == 2 and item[0] == 1 and isinstance(item[1], str):
            fragments.append(item[1])
    records = []
    for line in "".join(fragments).splitlines():
        _key, separator, body = line.partition(":")
        if not separator or not body.startswith(("{", "[")):
            continue
        try:
            records.append(json.loads(body))
        except (ValueError, RecursionError):
            continue
    return records


def parse_build_metadata(html: str) -> dict[str, dict]:
    records = _flight_records(html)
    metadata = {}
    visited = 0
    while records:
        item = records.pop()
        visited += 1
        if visited > 50000:
            raise ValueError("catalog metadata exceeds nesting budget")
        if isinstance(item, dict):
            found = _resource_metadata(item)
            if found is not None:
                model_id, fields = found
                metadata[model_id] = fields
            records.extend(item.values())
        elif isinstance(item, list):
            records.extend(item)
    if not metadata:
        raise ValueError("no hosted chat metadata found in catalog page")
    return metadata


def _schema_ref(spec: dict, schema: dict) -> dict:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    if not reference.startswith("#/"):
        return {}
    try:
        resolved = spec
        for key in reference[2:].split("/"):
            resolved = resolved[key.replace("~1", "/").replace("~0", "~")]
        return resolved if isinstance(resolved, dict) else {}
    except (KeyError, TypeError):
        return {}


def parse_detail_api_ids(html: str) -> set[str]:
    """Only model defaults in official hosted chat request schemas count."""
    records = _flight_records(html)
    found = set()
    visited = 0
    while records:
        item = records.pop()
        visited += 1
        if visited > 50000:
            raise ValueError("detail metadata exceeds nesting budget")
        if isinstance(item, dict):
            if "openapi" in item and "paths" in item:
                try:
                    servers = {
                        server.get("url", "").rstrip("/") for server in item.get("servers", [])
                    }
                    if "https://integrate.api.nvidia.com/v1" in servers:
                        request = item["paths"]["/chat/completions"]["post"]["requestBody"][
                            "content"
                        ]["application/json"]["schema"]
                        request = _schema_ref(item, request)
                        model = _schema_ref(item, request.get("properties", {}).get("model", {}))
                        default = model.get("default")
                        if isinstance(default, str) and _ID.fullmatch(default):
                            found.add(default)
                except (KeyError, TypeError, AttributeError):
                    pass
            records.extend(item.values())
        elif isinstance(item, list):
            records.extend(item)
    return found


async def _public_html(client: httpx.AsyncClient, url: str) -> str:
    request = client.build_request("GET", url, timeout=5)
    # No inference credentials, client auth or browser cookies cross hosts.
    request.headers = httpx.Headers(
        {"Host": "build.nvidia.com", "Accept": "text/html", "User-Agent": "openvidia-catalog"}
    )
    response = await client.send(request, stream=True, auth=None, follow_redirects=False)
    try:
        response.raise_for_status()
        chunks, total = [], 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_HTML_BYTES:
                raise ValueError("catalog page exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        await response.aclose()


class BuildCatalog:
    """One-hour metadata cache; last known good data survives fetch failures."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else config.config_dir() / "build-catalog.json"
        self.models: dict[str, dict] = {}
        self.refreshed_at = 0.0
        self.attempted_at = 0.0
        self.needs_resolution = True
        self.lock = asyncio.Lock()
        try:
            cached = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                return
            if cached.get("source") == SOURCE and isinstance(cached.get("models"), dict):
                timestamp = float(cached["refreshed_at"])
                if not math.isfinite(timestamp) or not 0 <= timestamp <= time.time() + 60:
                    return
                self.refreshed_at = timestamp
                self.needs_resolution = cached.get("schema_version") != 2
                self.models = {
                    model_id: fields
                    for model_id, fields in cached["models"].items()
                    if _ID.fullmatch(model_id)
                    and isinstance(fields, dict)
                    and fields.get("source") == SOURCE
                    and fields.get("endpoint_kind") == "free_chat"
                }
        except (OSError, ValueError, TypeError, KeyError):
            pass

    async def _refresh(self, client: httpx.AsyncClient, api_ids: set[str]) -> None:
        now = time.time()
        if (
            not self.needs_resolution and now - self.refreshed_at < TTL_SECONDS
        ) or now - self.attempted_at < 60:
            return
        async with self.lock:
            now = time.time()
            if (
                not self.needs_resolution and now - self.refreshed_at < TTL_SECONDS
            ) or now - self.attempted_at < 60:
                return
            self.attempted_at = now
            try:
                parsed = None

                async def resolve(resource_id: str) -> None:
                    try:
                        url = "https://build.nvidia.com/" + resource_id
                        candidates = parse_detail_api_ids(await _public_html(client, url))
                        candidates = {
                            candidate
                            for candidate in candidates
                            if candidate in api_ids
                            and candidate.split("/", 1)[0] == resource_id.split("/", 1)[0]
                        }
                        if len(candidates) == 1:
                            canonical = next(iter(candidates))
                            parsed[canonical] = {**parsed[resource_id], "api_model_source": url}
                    except (httpx.HTTPError, ValueError, TypeError, OSError):
                        pass

                try:
                    # Listing and at most four detail fetches share one deadline.
                    async with asyncio.timeout(6):
                        parsed = parse_build_metadata(await _public_html(client, SOURCE))
                        unmatched = sorted(
                            (resource_id for resource_id in parsed if resource_id not in api_ids),
                            key=lambda resource_id: parsed[resource_id]["updated_at"],
                            reverse=True,
                        )[:4]
                        await asyncio.gather(*(resolve(resource_id) for resource_id in unmatched))
                except TimeoutError:
                    if parsed is None:
                        return
                config.atomic_write(
                    self.path,
                    json.dumps(
                        {
                            "schema_version": 2,
                            "source": SOURCE,
                            "refreshed_at": now,
                            "models": parsed,
                        }
                    ),
                )
                self.models, self.refreshed_at = parsed, now
                self.needs_resolution = False
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError, OSError):
                # Optional descriptive metadata must never break the API list.
                return

    async def enrich(
        self, client: httpx.AsyncClient, models: list[dict]
    ) -> tuple[list[dict], dict]:
        await self._refresh(
            client, {model["id"] for model in models if isinstance(model.get("id"), str)}
        )
        enriched = []
        matched = 0
        for model in models:
            entry = dict(model)
            fields = self.models.get(entry.get("id"))
            if fields is not None:
                entry["catalog"] = dict(fields)
                matched += 1
            enriched.append(entry)
        return enriched, {
            "source": SOURCE,
            "scope": "latest_page",
            "refreshed_at": datetime.fromtimestamp(self.refreshed_at, UTC).isoformat()
            if self.refreshed_at
            else None,
            "stale": time.time() - self.refreshed_at >= TTL_SECONDS,
            "matched_models": matched,
        }


_caches: weakref.WeakKeyDictionary[httpx.AsyncClient, BuildCatalog] = weakref.WeakKeyDictionary()


async def enrich_models(client: httpx.AsyncClient, models: list[dict]) -> tuple[list[dict], dict]:
    path = config.config_dir() / "build-catalog.json"
    cache = _caches.get(client)
    if cache is None or cache.path != path:
        cache = BuildCatalog(path)
        _caches[client] = cache
    return await cache.enrich(client, models)
