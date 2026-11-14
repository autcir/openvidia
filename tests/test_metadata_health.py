"""Model metadata cannot disprove inference cooldowns or authentication errors."""

from __future__ import annotations

import httpx
import pytest

from openvidia.proxy_app import _health_check_all
from openvidia.proxy_state import ProxyState, ProxyStats, RpmTracker

KEY = "nvapi-test"


def snapshot(state):
    key = state.key_states[KEY]
    cooldown = state.cooldowns.get(KEY)
    return {
        "until": cooldown.until if cooldown else None,
        "status": cooldown.status if cooldown else None,
        "reason": cooldown.reason if cooldown else None,
        "key_until": key.cooldown_until,
        "valid": key.is_valid,
        "failures": key.consecutive_failures,
        "last_failure": key.last_failure_at,
        "last_success": key.last_success_at,
        "rpm_ceiling": state.rpm[KEY].max_rpm,
    }


def make_state():
    state = ProxyState([KEY], ProxyStats(), lambda _line: None)
    state.rpm[KEY] = RpmTracker()
    state.rpm[KEY].max_rpm = 14
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("status", [0, 401, 403, 429, 500, 502, 503, 504])
async def test_metadata_success_never_clears_inference_cooldown(force, status):
    state = make_state()
    state.mark_key_failed(KEY, status=status, retry_after=20)
    before = snapshot(state)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"data": []}))
    ) as client:
        await _health_check_all(state, client, force=force)
    assert snapshot(state) == before
    assert state.is_key_on_cooldown(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 401, 403, 503, 504])
async def test_failure_during_forced_metadata_request_survives_success(status):
    state = make_state()
    expected = []

    def respond(_request):
        # The metadata request began with a healthy key; inference failed while
        # it was in flight. A successful GET must not overwrite the newer fact.
        state.mark_key_failed(KEY, status=status, retry_after=20)
        expected.append(snapshot(state))
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await _health_check_all(state, client, force=True)
    assert snapshot(state) == expected[0]
    assert state.is_key_on_cooldown(KEY)


@pytest.mark.asyncio
async def test_new_failure_during_regular_metadata_request_is_not_replaced():
    state = make_state()
    state.mark_key_failed(KEY, status=503, retry_after=20)
    expected = []

    def respond(_request):
        state.mark_key_failed(KEY, status=504, retry_after=30)
        expected.append(snapshot(state))
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await _health_check_all(state, client)
    assert snapshot(state) == expected[0]
    assert state.cooldown_status(KEY) == 504


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_status", [200, 403, 429, 503])
async def test_forced_metadata_result_does_not_mutate_a_healthy_key(metadata_status):
    state = make_state()
    before = snapshot(state)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(metadata_status))
    ) as client:
        await _health_check_all(state, client, force=True)
    assert snapshot(state) == before
    assert not state.is_key_on_cooldown(KEY)


@pytest.mark.asyncio
async def test_metadata_connection_failure_does_not_replace_existing_backoff():
    state = make_state()
    state.mark_key_failed(KEY, status=503, retry_after=20)
    before = snapshot(state)

    def fail(request):
        raise httpx.ReadTimeout("metadata connection timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        await _health_check_all(state, client, force=True)
    assert snapshot(state) == before
