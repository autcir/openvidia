"""Tests for model adaptation, dynamic fallback, 529 retry, and payload normalization."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from openvidia.model_profiles import get_profile
from openvidia.proxy_state import ProxyState, ProxyStats
from openvidia.responses_shim import _rotation_phase, get_fallback_model


def _make_state(keys=None):
    return ProxyState(keys or ["k1"], ProxyStats(), lambda msg: None)


def test_get_fallback_model_prefers_starred():
    state = _make_state()
    # Mock presets
    mock_store = MagicMock()
    mock_store.load.return_value = {
        "nvidia/nemotron-3-ultra-550b-a55b": {"status": "passed"},
        "nvidia/nemotron-3.5-bolt-e4b": {"status": "passed"},
    }

    import openvidia.config as config

    orig_presets = config.load_saved_presets
    try:
        config.load_saved_presets = lambda: ["stepfun-ai/step-5-preview"]
        fb = get_fallback_model(state, "moonshotai/kimi-k3.1-pro", mock_store)
        assert fb == "stepfun-ai/step-5-preview"
    finally:
        config.load_saved_presets = orig_presets


def test_get_fallback_model_uses_validated_when_no_presets():
    state = _make_state()
    mock_store = MagicMock()
    mock_store.load.return_value = {
        "nvidia/nemotron-3-ultra-550b-a55b": {"status": "passed"},
        "stepfun-ai/step-5-preview": {"status": "passed"},
    }

    import openvidia.config as config

    orig_presets = config.load_saved_presets
    try:
        config.load_saved_presets = lambda: []
        fb = get_fallback_model(state, "moonshotai/kimi-k3.1-pro", mock_store)
        assert fb == "nvidia/nemotron-3-ultra-550b-a55b"
    finally:
        config.load_saved_presets = orig_presets


def test_get_fallback_model_skips_circuit_open():
    import time

    from openvidia.proxy_state import ModelHealth

    state = _make_state()
    h = ModelHealth()
    h.consecutive_failures = h.CIRCUIT_OPEN_AFTER
    h.circuit_opened_at = time.time()
    state.model_health["nvidia/nemotron-3-ultra-550b-a55b"] = h
    mock_store = MagicMock()
    mock_store.load.return_value = {
        "nvidia/nemotron-3-ultra-550b-a55b": {"status": "passed"},
        "stepfun-ai/step-5-preview": {"status": "passed"},
    }

    import openvidia.config as config

    orig_presets = config.load_saved_presets
    try:
        config.load_saved_presets = lambda: []
        fb = get_fallback_model(state, "moonshotai/kimi-k3.1-pro", mock_store)
        assert fb == "stepfun-ai/step-5-preview"
    finally:
        config.load_saved_presets = orig_presets


def test_mistral_payload_normalization_in_rotation():
    profile = get_profile("mistralai/mistral-large-4-1t-a52b")
    payload = {
        "model": "mistralai/mistral-large-4-1t-a52b",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "tool_choice": "none",
    }
    normalized = profile.normalize_payload(payload)
    assert "tool_choice" not in normalized


@pytest.mark.asyncio
async def test_529_retry_without_cooling_key():
    state = _make_state()
    call_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(529, text="Service temporarily overloaded", request=request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}, request=request
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        payload = {
            "model": "minimaxai/minimax-m3.1-flash",
            "messages": [{"role": "user", "content": "hi"}],
        }
        resp, used_key, idx = await _rotation_phase(
            client=client,
            method="POST",
            upstream="https://upstream.test/v1/chat/completions",
            payload=payload,
            headers_factory=lambda k, i: {"Authorization": f"Bearer {k}"},
            state=state,
            candidates=[(0, "k1")],
            max_attempts=3,
            timeout=5.0,
            stream=False,
            log_tag="test-529",
            outcome_box={},
        )
        assert resp is not None
        assert resp.status_code == 200
        assert call_count == 2
        # Key should not be on cooldown
        assert state.is_key_healthy("k1")
