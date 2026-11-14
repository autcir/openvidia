"""Fresh local evidence, not a frozen list of model recommendations."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from openvidia import config, proxy_app, soak
from openvidia.model_validation import TTL, ValidationStore
from openvidia.proxy_state import ProxyState, ProxyStats

MODEL = "provider/current"
PASS = {"ok": True, "stages": {"stream": True, "tool_call": True, "tool_result": True}}


def test_latest_failure_replaces_passed_client_evidence(tmp_path):
    store = ValidationStore(tmp_path / "checks.json")
    row = store.record(MODEL, PASS)
    assert row["status"] == "passed"
    assert row["opencode"] is False
    assert (
        datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["tested_at"]) == TTL
    )
    assert store.record_opencode(MODEL, True)["opencode"] is True
    row = store.record(MODEL, {"ok": False, "stages": {}, "error_type": "TimeoutError"})
    assert row["status"] == "failed"
    assert row["reason"] == "timeout"
    assert store.load()[MODEL]["opencode"] is False


def test_partial_protocol_does_not_become_a_pass(tmp_path):
    row = ValidationStore(tmp_path / "checks.json").record(
        MODEL, {"ok": True, "stages": {"stream": True, "tool_call": True}}
    )
    assert row["status"] == "failed"
    assert row["tool_result"] is False


def test_expired_evidence_is_visible_but_cannot_get_fresh_client_badge(tmp_path):
    store = ValidationStore(tmp_path / "checks.json")
    now = datetime.now(UTC) - timedelta(hours=7)
    store.path.write_text(
        json.dumps(
            {
                "models": {
                    MODEL: {
                        "status": "passed",
                        "tested_at": now.isoformat(),
                        "expires_at": (now + TTL).isoformat(),
                        "stream": True,
                        "tool_call": True,
                        "tool_result": True,
                        "opencode": True,
                    }
                }
            }
        )
    )
    assert store.load()[MODEL]["expires_at"] < datetime.now(UTC).isoformat()
    with pytest.raises(ValueError, match="fresh protocol"):
        store.record_opencode(MODEL, True)


def test_corrupt_or_unbounded_lifetime_evidence_cannot_recommend_models(tmp_path):
    store = ValidationStore(tmp_path / "checks.json")
    store.path.write_text('{"models": []}')
    assert store.load() == {}
    row = store.record(MODEL, PASS)
    row["expires_at"] = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    store.path.write_text(json.dumps({"models": {MODEL: row}}))
    assert store.load() == {}


@pytest.mark.asyncio
async def test_control_check_is_authenticated_and_persists_latest_outcome(monkeypatch):
    constructor = httpx.AsyncClient
    upstream = constructor(transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    def client_factory(**kwargs):
        if isinstance(kwargs.get("transport"), httpx.ASGITransport):
            return constructor(**kwargs)
        return upstream

    monkeypatch.setattr(proxy_app.httpx, "AsyncClient", client_factory)
    app = proxy_app.create_app(ProxyState(["test-key"], ProxyStats(), lambda _: None))
    app.state.probe_transport = httpx.ASGITransport(app=app)
    outcomes = [PASS, {"ok": False, "stages": {}, "error_type": "TimeoutError"}]

    async def probe(_client, model, timeout):
        assert model == MODEL
        assert timeout == 60
        return outcomes.pop(0)

    monkeypatch.setattr(soak, "probe", probe)
    try:
        async with constructor(
            transport=httpx.ASGITransport(app), base_url="http://localhost:1919"
        ) as client:
            assert (
                await client.post("/api/validate-model", json={"model": MODEL})
            ).status_code == 401
            client.headers["x-openvidia-token"] = config.control_token()
            assert (
                await client.post("/api/validate-model", json={"model": "openvidia"})
            ).status_code == 400
            first = (await client.post("/api/validate-model", json={"model": MODEL})).json()
            assert first["ok"] is True and first["validation"]["status"] == "passed"
            second = (await client.post("/api/validate-model", json={"model": MODEL})).json()
            assert second["ok"] is True and second["validation"]["status"] == "failed"
            assert ValidationStore().load()[MODEL]["status"] == "failed"
    finally:
        await upstream.aclose()


def test_out_of_range_timezone_is_ignored(tmp_path):
    store = ValidationStore(tmp_path / "checks.json")
    row = store.record(MODEL, PASS)
    row["tested_at"] = "9999-12-31T23:59:59-01:00"
    store.path.write_text(json.dumps({"models": {MODEL: row}}))
    assert store.load() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_keys", [True, False])
async def test_strict_model_checks_cannot_certify_fallback(monkeypatch, with_keys):
    constructor = httpx.AsyncClient
    upstream = constructor(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    monkeypatch.setattr(proxy_app.httpx, "AsyncClient", lambda **_: upstream)
    monkeypatch.setattr(config, "model_options", lambda: {"fallback": "next"})
    monkeypatch.setattr(config, "load_saved_presets", lambda: ["provider/other"])

    def unexpected(*args, **kwargs):
        pytest.fail("Strict model check must not execute a configured provider fallback")

    monkeypatch.setattr(proxy_app, "load_provider_configs", unexpected)
    state = ProxyState(["test-key"] if with_keys else [], ProxyStats(), lambda _: None)
    if with_keys:
        from openvidia.proxy_state import ModelHealth

        health = ModelHealth()
        for _ in range(health.CIRCUIT_OPEN_AFTER):
            health.record_failure()
        state.model_health[MODEL] = health
    app = proxy_app.create_app(state)
    try:
        async with constructor(
            transport=httpx.ASGITransport(app), base_url="http://localhost:1919"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": MODEL, "messages": []},
                headers={"X-OpenVidia-Strict-Model": "1"},
            )
            assert response.status_code == 503
    finally:
        await upstream.aclose()


def test_validation_cache_is_private_atomic_and_does_not_fsync(monkeypatch, tmp_path):
    import os
    import stat

    def unexpected(*args):
        pytest.fail("Expiring validation evidence must not fsync on the request path")

    monkeypatch.setattr(os, "fsync", unexpected)
    store = ValidationStore(tmp_path / "checks.json")
    store.record(MODEL, PASS)
    assert store.load()[MODEL]["status"] == "passed"
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".model-validation-*"))
