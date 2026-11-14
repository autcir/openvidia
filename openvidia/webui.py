"""Embedded control panel served by the proxy itself.

A thin FastAPI router mounted on the same process that runs the proxy:
serves the static dashboard bundle plus a small REST/SSE surface for
key management, stats, presets and live logs. All mutators persist via
``config`` so state survives restarts; reads mirror ``ProxyState``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import webbrowser
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import config
from .proxy_state import ProxyState

MODEL_PROBE_TIMEOUT = 45.0


# --------------------------------------------------------------------------- #
# Static dashboard bundle
# --------------------------------------------------------------------------- #


def attach_webui(app: FastAPI, state: ProxyState, web_dir: Path) -> None:
    """Register the dashboard + management API on ``app``.

    Routes are closures over ``state`` and ``web_dir`` so the single
    ``ProxyState`` instance stays the source of truth for both the proxy
    and the UI.
    """

    @app.get("/")
    async def index() -> HTMLResponse:
        p = web_dir / "index.html"
        if not p.exists():
            return HTMLResponse("<h1>OpenVidia</h1><p>UI not found</p>", status_code=404)
        return HTMLResponse(p.read_text())

    @app.get("/styles.css")
    async def styles() -> Response:
        p = web_dir / "styles.css"
        return (
            Response(content=p.read_bytes(), media_type="text/css")
            if p.exists()
            else Response("", status_code=404)
        )

    @app.get("/main.js")
    async def main_js() -> Response:
        p = web_dir / "main.js"
        return (
            Response(content=p.read_bytes(), media_type="application/javascript")
            if p.exists()
            else Response("", status_code=404)
        )

    @app.get("/logo.png")
    async def logo() -> Response:
        for p in [web_dir / "logo.png", web_dir / "assets" / "logo.png"]:
            if p.exists():
                return Response(content=p.read_bytes(), media_type="image/png")
        return Response("", status_code=404)

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        for p in [
            web_dir / "favicon.ico",
            web_dir / "assets" / "favicon.ico",
            web_dir / "assets" / "logo.png",
        ]:
            if p.exists():
                return Response(content=p.read_bytes(), media_type="image/x-icon")
        return Response("", status_code=404)

    # ----------------------------------------------------------------------- #
    # Health & status
    # ----------------------------------------------------------------------- #

    @app.get("/api/status")
    async def api_status() -> dict:
        async with state.lock:
            on_cooldown = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
        return {
            "running": state.running,
            "port": state.port,
            "keys": len(state.keys),
            "cooldowns": on_cooldown,
            "upstream": dict(state.upstream),
        }

    @app.get("/api/stats")
    async def api_stats() -> dict:
        async with state.lock:
            on_cooldown = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
            total_rpm = sum(state.key_rpm(k) for k in state.keys)
            n_healthy = sum(1 for k in state.keys if state.is_key_healthy(k))
            in_flight = sum(
                (state._key_states.get(k).in_flight if state._key_states.get(k) else 0)
                for k in state.keys
            )
            agg_ceiling = sum(
                (state.rpm.get(k).max_rpm if state.rpm.get(k) and state.rpm.get(k).max_rpm else 28)
                for k in state.keys
            )
        return {
            "requests": state.stats.requests,
            "rotations": state.stats.rotations,
            "success": state.stats.success,
            "active_index": state.stats.active_key_index,
            "cooldowns": on_cooldown,
            "total_rpm": total_rpm,
            "healthy": n_healthy,
            "in_flight": in_flight,
            "aggregate_rpm_ceiling": agg_ceiling,
        }

    # ----------------------------------------------------------------------- #
    # Keys CRUD
    # ----------------------------------------------------------------------- #

    # The dashboard never holds a usable key. It works entirely in masked
    # strings plus an index, so a bug in the page — or a script that got past
    # the origin guard — has nothing to steal. Copy-to-clipboard is the one
    # operation that genuinely needs cleartext, and it asks for one key at a
    # time through its own route.

    @app.get("/api/keys")
    async def api_get_keys() -> dict:
        async with state.lock:
            return {"keys": [config.mask_key(k) for k in state.keys]}

    @app.post("/api/keys/reveal")
    async def api_reveal_key(request: Request) -> dict:
        """Return one key in cleartext, by index, for the copy button."""
        body = await request.json()
        idx = body.get("index")
        if not isinstance(idx, int):
            return {"ok": False, "error": "index required"}
        async with state.lock:
            if not 0 <= idx < len(state.keys):
                return {"ok": False, "error": "index out of range"}
            return {"ok": True, "key": state.keys[idx]}

    @app.post("/api/keys/add")
    async def api_add_key(request: Request) -> dict:
        body = await request.json()
        key = body.get("key", "")
        if not isinstance(key, str) or not key.strip():
            return {"ok": False, "error": "key required"}
        key = key.strip()
        async with state.lock:
            # Duplicates would each get their own index while sharing a single
            # KeyState, which double-counts in-flight and misattributes errors.
            if key in state.keys:
                return {"ok": False, "error": "key already in pool"}
            state.keys = list(state.keys) + [key]
            keys = list(state.keys)
        config.save_keys_file(keys)
        return {"ok": True, "keys": [config.mask_key(k) for k in keys]}

    @app.post("/api/keys/remove")
    async def api_remove_key(request: Request) -> dict:
        body = await request.json()
        idx = body.get("index")
        if not isinstance(idx, int):
            return {"ok": False, "error": "index required"}
        async with state.lock:
            if not 0 <= idx < len(state.keys):
                return {"ok": False, "error": "index out of range"}
            state.keys = [k for i, k in enumerate(state.keys) if i != idx]
            keys = list(state.keys)
        config.save_keys_file(keys)
        return {"ok": True, "keys": [config.mask_key(k) for k in keys]}

    # ----------------------------------------------------------------------- #
    # Per-key live stats (cooldown, RPM, validity, freshness)
    # ----------------------------------------------------------------------- #

    @app.get("/api/keys/stats")
    async def api_key_stats() -> dict:
        async with state.lock:
            stats: dict[str, dict] = {}
            now = time.time()
            for i, k in enumerate(state.keys):
                u = state.stats.key_usage.get(k)
                entry: dict = {}
                if u:
                    entry.update(
                        {
                            "requests": u.requests,
                            "success": u.success,
                            "failed": u.failed,
                            "last_used": u.last_used,
                            "last_error": u.last_error,
                            "freshness": "fresh"
                            if (now - u.last_used) < 120
                            else "stale"
                            if u.last_used > 0
                            else "unused",
                        }
                    )
                # Cooldown remaining, current RPM, last probe validity,
                # adaptive RPM ceiling, in-flight count and consecutive failures
                # (used by the dashboard to show adaptive backoff state).
                cd_rem = state.cooldown_remaining(k)
                entry["cooldown"] = round(cd_rem, 1) if cd_rem > 0 else 0
                entry["cooldown_reason"] = state.cooldown_reason(k) if cd_rem > 0 else ""
                entry["rpm"] = state.key_rpm(k)
                tracker = state.rpm.get(k)
                entry["rpm_ceiling"] = tracker.max_rpm if tracker and tracker.max_rpm else 28
                ks = state.key_states.get(k)
                entry["is_valid"] = ks.is_valid if ks else True
                entry["in_flight"] = ks.in_flight if ks else 0
                entry["consecutive_failures"] = ks.consecutive_failures if ks else 0
                stats[str(i)] = entry
            return {
                "active_index": state.stats.active_key_index,
                "key_stats": stats,
            }

    # ----------------------------------------------------------------------- #
    # Active model
    # ----------------------------------------------------------------------- #

    @app.get("/api/model")
    async def api_get_model() -> dict:
        return {"model": state.active_model or ""}

    @app.post("/api/model")
    async def api_set_model(request: Request) -> dict:
        body = await request.json()
        raw = body.get("model", "")
        if not isinstance(raw, str):
            return {"ok": False, "error": "model must be a string"}
        m = raw.strip() or None
        if m is not None and not config.is_valid_model_id(m):
            return {"ok": False, "error": "invalid model id"}
        state.active_model = m
        config.save_active_model(m or "")
        return {"ok": True, "model": m or ""}

    @app.get("/api/thinking")
    async def api_get_thinking() -> dict:
        opts = config.model_options()
        model = state.active_model or ""
        per = (opts.get("per_model") or {}).get(model, {})
        return {
            "model": model,
            "mode": per.get("thinking") or opts.get("thinking", "auto"),
            "inherited": "thinking" not in per,
        }

    @app.post("/api/thinking")
    async def api_set_thinking(request: Request) -> dict:
        """Set the reasoning mode for the active model (or globally).

        A hybrid reasoning model emits nothing until it stops thinking, which
        is the difference between a 2s and a 160s first token — worth a switch
        rather than a config file edit.
        """
        body = await request.json()
        mode = body.get("mode", "auto")
        if mode not in ("auto", "on", "off"):
            return {"ok": False, "error": "mode must be auto, on or off"}
        opts = config.model_options()
        model = body.get("model", state.active_model or "")
        if model:
            per = dict(opts.get("per_model") or {})
            entry = dict(per.get(model) or {})
            if mode == "auto":
                entry.pop("thinking", None)
            else:
                entry["thinking"] = mode
            if entry:
                per[model] = entry
            else:
                per.pop(model, None)
            opts["per_model"] = per
        else:
            opts["thinking"] = mode
        config.save_model_options(opts)
        state.log_cb(f"◆ thinking={mode} for {model or 'all models'}")
        return {"ok": True, "model": model, "mode": mode}

    @app.get("/api/reasoning-effort")
    async def api_get_effort() -> dict:
        opts = config.model_options()
        model = state.active_model or ""
        per = (opts.get("per_model") or {}).get(model, {})
        return {
            "model": model,
            "effort": per.get("reasoning_effort") or opts.get("reasoning_effort", "auto"),
            "inherited": "reasoning_effort" not in per,
        }

    @app.post("/api/reasoning-effort")
    async def api_set_effort(request: Request) -> dict:
        """Set reasoning effort: low (fast, no thinking), medium (balanced), high (deep)."""
        body = await request.json()
        effort = body.get("effort", "auto")
        if effort not in ("auto", "low", "medium", "high"):
            return {"ok": False, "error": "effort must be auto, low, medium or high"}
        opts = config.model_options()
        model = body.get("model", state.active_model or "")
        if model:
            per = dict(opts.get("per_model") or {})
            entry = dict(per.get(model) or {})
            if effort == "auto":
                entry.pop("reasoning_effort", None)
            else:
                entry["reasoning_effort"] = effort
            if entry:
                per[model] = entry
            else:
                per.pop(model, None)
            opts["per_model"] = per
        else:
            opts["reasoning_effort"] = effort
        config.save_model_options(opts)
        state.log_cb(f"◆ reasoning_effort={effort} for {model or 'all models'}")
        return {"ok": True, "model": model, "effort": effort}

    @app.get("/api/fallback")
    async def api_get_fallback() -> dict:
        opts = config.model_options()
        model = state.active_model or ""
        per = (opts.get("per_model") or {}).get(model, {})
        return {
            "model": model,
            "fallback": per.get("fallback") or opts.get("fallback", "off"),
            "inherited": "fallback" not in per,
        }

    @app.post("/api/fallback")
    async def api_set_fallback(request: Request) -> dict:
        """Set fallback mode: off (never failover), on (always failover), auto (default behavior)."""
        body = await request.json()
        mode = body.get("fallback", "off")
        if mode not in ("off", "on", "auto"):
            return {"ok": False, "error": "fallback must be off, on or auto"}
        opts = config.model_options()
        model = body.get("model", state.active_model or "")
        if model:
            per = dict(opts.get("per_model") or {})
            entry = dict(per.get(model) or {})
            if mode == "auto":
                entry.pop("fallback", None)
            else:
                entry["fallback"] = mode
            if entry:
                per[model] = entry
            else:
                per.pop(model, None)
            opts["per_model"] = per
        else:
            opts["fallback"] = mode
        config.save_model_options(opts)
        state.log_cb(f"◆ fallback={mode} for {model or 'all models'}")
        return {"ok": True, "model": model, "fallback": mode}

    @app.get("/api/model-health")
    async def api_model_health() -> dict:
        """What the proxy has learned about each model from real traffic."""
        return {m: h.as_dict() for m, h in sorted(state.model_health.items())}

    # ----------------------------------------------------------------------- #
    # Lifecycle: stop / start / restart
    # ----------------------------------------------------------------------- #

    @app.post("/api/stop")
    async def api_stop() -> dict:
        state.running = False
        return {"ok": True, "status": "stopped"}

    @app.post("/api/start")
    async def api_start() -> dict:
        state.running = True
        return {"ok": True, "status": "running"}

    @app.post("/api/restart")
    async def api_restart() -> Response:
        request_restart = getattr(app.state, "request_restart", None)
        if request_restart is None:
            return Response(
                content=json.dumps(
                    {"ok": False, "error": "restart requires the foreground process owner"}
                ),
                media_type="application/json",
                status_code=409,
            )
        # Return the acknowledgement before the owner drains the server.
        asyncio.get_running_loop().call_later(0.2, request_restart)
        return Response(
            content=json.dumps({"ok": True, "restarting": True}),
            media_type="application/json",
        )

    # ----------------------------------------------------------------------- #
    # Model probe — exercise the same route and policies as local clients
    # ----------------------------------------------------------------------- #

    @app.post("/api/test-model")
    async def api_test_model(request: Request) -> Response:
        def failure(code: str, detail: str, *, model: str | None = None, status: int = 200):
            result = {"ok": False, "error": code, "detail": detail}
            if model is not None:
                result["model"] = model
            return JSONResponse(result, status_code=status)

        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            return failure("invalid_request", "Send a JSON object with a model ID.", status=400)
        model_id = body.get("model") if isinstance(body, dict) else None
        if not isinstance(model_id, str) or not config.is_valid_model_id(model_id):
            return failure("invalid_request", "A valid model ID is required.", status=400)
        async with state.lock:
            if not state.keys:
                return failure("no_keys", "No API keys are configured.", model=model_id)

        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply exactly OK."}],
            "max_tokens": 256,
            "stream": False,
        }
        try:
            # A fixed loopback HTTP request exercises the ordinary proxy while
            # enforcing socket cancellation independently of its ASGI cleanup.
            # No external base URL, credentials or environment proxy is accepted.
            async with asyncio.timeout(MODEL_PROBE_TIMEOUT):
                async with httpx.AsyncClient(
                    transport=getattr(app.state, "probe_transport", None),
                    base_url=f"http://127.0.0.1:{state.port}",
                    timeout=MODEL_PROBE_TIMEOUT,
                    trust_env=False,
                ) as local:
                    response = await local.post(
                        "/v1/chat/completions",
                        json=payload,
                        headers={"X-OpenVidia-Strict-Model": "1"},
                    )
            status = response.status_code
            if status == 429:
                return failure(
                    "rate_limited", "The provider is rate limited; retry later.", model=model_id
                )
            if status in (408, 504):
                return failure(
                    "timeout", "The model did not answer within the probe deadline.", model=model_id
                )
            if status in (401, 403):
                return failure(
                    "authentication_failed",
                    "The provider rejected the configured credentials.",
                    model=model_id,
                )
            if status in (400, 404, 413, 422):
                return failure(
                    "rejected", "The provider rejected this model or probe request.", model=model_id
                )
            if not response.is_success:
                return failure(
                    "unavailable",
                    "The model is temporarily unavailable through the proxy.",
                    model=model_id,
                )
            data = response.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return failure(
                    "invalid_response",
                    "The provider returned no valid completion choice.",
                    model=model_id,
                )
            message = choices[0].get("message")
            if not isinstance(message, dict):
                return failure(
                    "invalid_response",
                    "The provider returned no valid completion message.",
                    model=model_id,
                )
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                return failure(
                    "invalid_response",
                    "The provider returned an unsupported completion format.",
                    model=model_id,
                )
            if not content or not content.strip():
                if message.get("tool_calls"):
                    return failure(
                        "tool_only",
                        "The model returned tool calls instead of a text answer.",
                        model=model_id,
                    )
                if message.get("reasoning_content") or message.get("reasoning"):
                    return failure(
                        "reasoning_only",
                        "The model produced reasoning without a text answer within the probe budget.",
                        model=model_id,
                    )
                return failure(
                    "empty_response", "The model returned no usable text answer.", model=model_id
                )
            return JSONResponse(
                {
                    "ok": True,
                    "model": model_id,
                    "response": "Usable text completion received.",
                    "output_chars": len(content),
                }
            )
        except (TimeoutError, httpx.TimeoutException):
            return failure(
                "timeout", "The model did not answer within the probe deadline.", model=model_id
            )
        except httpx.HTTPError:
            return failure(
                "connection_error",
                "The proxy could not complete the model request.",
                model=model_id,
            )
        except (ValueError, UnicodeError):
            return failure(
                "invalid_response",
                "The provider returned an invalid completion response.",
                model=model_id,
            )

    # ----------------------------------------------------------------------- #
    # Presets (saved key sets)
    # ----------------------------------------------------------------------- #

    @app.get("/api/presets")
    async def api_get_presets() -> dict:
        return {"presets": config.load_saved_presets()}

    @app.post("/api/presets")
    async def api_save_presets(request: Request) -> dict:
        body = await request.json()
        raw = body.get("presets", [])
        if not isinstance(raw, list):
            return {"ok": False, "error": "presets must be a list"}
        # Same rule as the active model: these ids are persisted and rendered.
        presets = [m for m in raw if isinstance(m, str) and config.is_valid_model_id(m)]
        if len(presets) != len(raw):
            return {"ok": False, "error": "invalid model id in presets"}
        config.save_presets_file(presets)
        return {"ok": True}

    # ----------------------------------------------------------------------- #
    # Live log stream (SSE)
    # ----------------------------------------------------------------------- #

    @app.get("/api/logs/stream")
    async def log_stream(request: Request) -> StreamingResponse:
        async def event_generator():
            queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
            # Subscribe first so events arriving while history is being sent
            # enter the live queue. Monotonic IDs remove any replay overlap.
            history = state.subscribe_logs(queue)
            last_id = 0
            try:
                for event in history:
                    last_id = max(last_id, event["id"])
                    yield f"data: {json.dumps(event)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=2.0)
                        if event["id"] <= last_id:
                            continue
                        last_id = event["id"]
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                state.unsubscribe_logs(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Convenience: open the dashboard in the user's default browser
# --------------------------------------------------------------------------- #


def auto_open(port: int = 1919) -> None:
    """Best-effort browser launch 1.5 s after call.

    The delay gives the proxy a head start so the browser doesn't race
    the listener and show a connection-refused page on slow machines.
    """
    url = f"http://localhost:{port}/?token={config.control_token()}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
