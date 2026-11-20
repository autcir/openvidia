"""Unit tests for openvidia.model_profiles."""

from __future__ import annotations

import copy

import pytest

from openvidia.model_profiles import (
    DEFAULT_PROFILE,
    EXACT_OVERRIDES,
    NAMESPACE_PROFILES,
    ModelProfile,
    get_profile,
    register_exact_profile,
    register_namespace_profile,
)


def test_model_profile_fields_and_defaults():
    profile = ModelProfile(name="test-model")
    assert profile.name == "test-model"
    assert profile.read_timeout == 60.0
    assert profile.max_529_retries == 1
    assert profile.backoff_529_s == 1.5
    assert profile.keepalive_interval_s == 15.0
    assert profile.supports_thinking is False


def test_default_profile_constant():
    assert DEFAULT_PROFILE.name == "default"
    assert DEFAULT_PROFILE.read_timeout == 60.0
    assert DEFAULT_PROFILE.max_529_retries == 1
    assert DEFAULT_PROFILE.backoff_529_s == 1.5
    assert DEFAULT_PROFILE.keepalive_interval_s == 15.0
    assert DEFAULT_PROFILE.supports_thinking is False


def test_get_profile_fallback_for_none_and_empty():
    assert get_profile(None).name == "default"
    assert get_profile("").name == "default"
    assert get_profile("   ").name == "default"
    assert get_profile("openvidia/openvidia").name == "default"
    assert get_profile("completely-unknown-model-xyz").name == "default"


def test_get_profile_returns_copy():
    p1 = get_profile(None)
    p1.read_timeout = 999.0
    assert DEFAULT_PROFILE.read_timeout == 60.0
    p2 = get_profile(None)
    assert p2.read_timeout == 60.0


@pytest.mark.parametrize(
    ("namespace", "expected_timeout", "expected_retries", "expected_thinking"),
    [
        ("mistralai/*", 90.0, 1, False),
        ("moonshotai/*", 180.0, 1, False),
        ("minimaxai/*", 60.0, 3, False),
        ("nvidia/*", 45.0, 1, True),
        ("google/*", 60.0, 1, False),
        ("stepfun-ai/*", 60.0, 1, False),
        ("z-ai/*", 60.0, 1, False),
    ],
)
def test_namespace_profiles_exist(namespace, expected_timeout, expected_retries, expected_thinking):
    assert namespace in NAMESPACE_PROFILES
    prof = NAMESPACE_PROFILES[namespace]
    assert prof.read_timeout == expected_timeout
    assert prof.max_529_retries == expected_retries
    assert prof.supports_thinking is expected_thinking
    assert prof.backoff_529_s == 1.5
    assert prof.keepalive_interval_s == 15.0


@pytest.mark.parametrize(
    ("model_id", "expected_timeout", "expected_retries", "expected_thinking"),
    [
        ("mistralai/mistral-large-4-1t-a52b", 90.0, 1, False),
        ("mistralai/mistral-small-24b-instruct-2501", 90.0, 1, False),
        ("moonshotai/kimi-k3_1-pro", 180.0, 1, False),
        ("minimaxai/minimax-text-01", 60.0, 3, False),
        ("nvidia/nemotron-4-340b-instruct", 45.0, 1, True),
        ("google/gemma-2-27b-it", 60.0, 1, False),
        ("stepfun-ai/step-2-16k", 60.0, 1, False),
        ("z-ai/glm-5.2", 60.0, 1, False),
    ],
)
def test_get_profile_namespace_matching(
    model_id, expected_timeout, expected_retries, expected_thinking
):
    prof = get_profile(model_id)
    assert prof.read_timeout == expected_timeout
    assert prof.max_529_retries == expected_retries
    assert prof.supports_thinking is expected_thinking


def test_get_profile_case_insensitivity():
    prof = get_profile("MistralAI/Mistral-Large-4-1t-A52B")
    assert prof.read_timeout == 90.0
    assert prof.max_529_retries == 1

    prof_kimi = get_profile("MOONSHOTAI/KIMI-K3_1-PRO")
    assert prof_kimi.read_timeout == 180.0


def test_keyword_heuristics():
    # Kimi / Moonshot heuristic -> 180s
    assert get_profile("kimi-pro").read_timeout == 180.0
    assert get_profile("custom/moonshot-preview").read_timeout == 180.0

    # Nemotron heuristic -> 45s, supports_thinking=True
    nem = get_profile("meta/llama-3.1-nemotron-70b-instruct")
    assert nem.read_timeout == 45.0
    assert nem.supports_thinking is True

    # Mistral heuristic -> 90s
    assert get_profile("custom/mistral-v1").read_timeout == 90.0

    # Minimax heuristic -> max_529_retries=3
    assert get_profile("other/minimax-v2").max_529_retries == 3


def test_exact_model_overrides():
    prof = get_profile("nvidia/nemotron-3-ultra-550b-a55b")
    assert prof.read_timeout == 45.0
    assert prof.supports_thinking is True

    prof_deepseek = get_profile("deepseek-ai/deepseek-v4.1-flash")
    assert prof_deepseek.read_timeout == 60.0
    assert prof_deepseek.supports_thinking is True


def test_register_exact_profile():
    custom_model = "test-vendor/custom-super-model"
    custom_profile = ModelProfile(
        name=custom_model,
        read_timeout=12.5,
        max_529_retries=7,
        backoff_529_s=2.5,
        keepalive_interval_s=5.0,
        supports_thinking=True,
    )
    register_exact_profile(custom_model, custom_profile)

    try:
        resolved = get_profile(custom_model)
        assert resolved.name == custom_model
        assert resolved.read_timeout == 12.5
        assert resolved.max_529_retries == 7
        assert resolved.backoff_529_s == 2.5
        assert resolved.keepalive_interval_s == 5.0
        assert resolved.supports_thinking is True
    finally:
        EXACT_OVERRIDES.pop(custom_model, None)


def test_register_namespace_profile():
    pattern = "customorg/*"
    custom_profile = ModelProfile(
        name=pattern,
        read_timeout=75.0,
        max_529_retries=5,
    )
    register_namespace_profile(pattern, custom_profile)

    try:
        resolved = get_profile("customorg/cool-model")
        assert resolved.read_timeout == 75.0
        assert resolved.max_529_retries == 5
    finally:
        NAMESPACE_PROFILES.pop(pattern, None)


def test_normalize_payload_mistral_strips_tool_choice_on_tool_message():
    profile = get_profile("mistralai/mistral-large-4-1t-a52b")

    payload_with_tool_role = {
        "model": "mistralai/mistral-large-4-1t-a52b",
        "messages": [
            {"role": "user", "content": "Calculate something"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function"}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "42"},
        ],
        "tool_choice": "none",
        "tools": [{"type": "function", "function": {"name": "calc"}}],
    }
    payload_copy = copy.deepcopy(payload_with_tool_role)

    normalized = profile.normalize_payload(payload_with_tool_role)

    # Immutability check
    assert payload_with_tool_role == payload_copy
    # tool_choice stripped
    assert "tool_choice" not in normalized
    # other fields preserved
    assert normalized["model"] == "mistralai/mistral-large-4-1t-a52b"
    assert len(normalized["messages"]) == 3
    assert len(normalized["tools"]) == 1


def test_normalize_payload_mistral_keeps_tool_choice_when_no_tool_role():
    profile = get_profile("mistralai/mistral-large-4-1t-a52b")

    payload = {
        "model": "mistralai/mistral-large-4-1t-a52b",
        "messages": [
            {"role": "user", "content": "Calculate something"},
        ],
        "tool_choice": "required",
        "tools": [{"type": "function", "function": {"name": "calc"}}],
    }

    normalized = profile.normalize_payload(payload)
    assert normalized.get("tool_choice") == "required"


def test_normalize_payload_non_mistral_keeps_tool_choice():
    # Google profile does not strip tool_choice even with role == 'tool'
    profile = get_profile("google/gemma-2-27b-it")

    payload = {
        "model": "google/gemma-2-27b-it",
        "messages": [
            {"role": "tool", "content": "result"},
        ],
        "tool_choice": "none",
    }

    normalized = profile.normalize_payload(payload)
    assert normalized.get("tool_choice") == "none"


def test_normalize_payload_strips_chat_template_kwargs_without_thinking():
    # Mistral does not support thinking
    profile = get_profile("mistralai/mistral-large-4-1t-a52b")
    assert profile.supports_thinking is False

    payload = {
        "model": "mistralai/mistral-large-4-1t-a52b",
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": "hi"}],
    }

    normalized = profile.normalize_payload(payload)
    assert "chat_template_kwargs" not in normalized

    # Also when enable_thinking is True on a model without thinking support
    payload_true = {
        "model": "mistralai/mistral-large-4-1t-a52b",
        "chat_template_kwargs": {"enable_thinking": True},
        "messages": [{"role": "user", "content": "hi"}],
    }
    normalized_true = profile.normalize_payload(payload_true)
    assert "chat_template_kwargs" not in normalized_true

    # Also when chat_template_kwargs is empty
    payload_empty = {
        "model": "mistralai/mistral-large-4-1t-a52b",
        "chat_template_kwargs": {},
        "messages": [{"role": "user", "content": "hi"}],
    }
    normalized_empty = profile.normalize_payload(payload_empty)
    assert "chat_template_kwargs" not in normalized_empty


def test_normalize_payload_preserves_chat_template_kwargs_with_thinking():
    # Nemotron supports thinking
    profile = get_profile("nvidia/nemotron-3-ultra-550b-a55b")
    assert profile.supports_thinking is True

    payload = {
        "model": "nvidia/nemotron-3-ultra-550b-a55b",
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": "hi"}],
    }

    normalized = profile.normalize_payload(payload)
    assert normalized.get("chat_template_kwargs") == {"enable_thinking": False}


def test_normalize_payload_graceful_edge_cases():
    profile = get_profile(None)
    assert profile.normalize_payload({}) == {}

    # Messages is not a list
    p1 = {"messages": "not-a-list"}
    assert profile.normalize_payload(p1) == p1

    # Messages contains non-dicts
    p2 = {"messages": [None, 123, "hello"]}
    assert profile.normalize_payload(p2) == p2
