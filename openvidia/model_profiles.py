"""Model-specific configuration profiles, timeouts, and payload normalization.

Provides per-model and per-provider tuning for NVIDIA NIM endpoints, including
read timeouts, 529 retry budgets, keepalive heartbeat intervals, and payload
normalization quirks (e.g. Mistral tool_choice omissions and thinking flag
stripping for non-reasoning models).
"""

from __future__ import annotations

import copy
import fnmatch
from dataclasses import dataclass


@dataclass
class ModelProfile:
    """Configuration profile for an upstream model or model namespace."""

    name: str
    read_timeout: float = 60.0
    max_529_retries: int = 1
    backoff_529_s: float = 1.5
    keepalive_interval_s: float = 15.0
    supports_thinking: bool = False

    def normalize_payload(self, payload: dict) -> dict:
        """Normalize outgoing request payload according to provider/model quirks.

        - For Mistral (mistralai/*): if any message has role == 'tool', delete
          'tool_choice' (or ensure omitted) so NVIDIA NIM does not hang on tool
          result turns.
        - For models without thinking support: strip 'chat_template_kwargs'
          if enable_thinking is false or irrelevant, preventing 400 rejection.
        """
        normalized = copy.deepcopy(payload)

        # 1. Mistral tool_choice workaround
        model_str = str(normalized.get("model") or self.name or "").lower()
        is_mistral = "mistral" in self.name.lower() or "mistral" in model_str
        if is_mistral:
            messages = normalized.get("messages")
            if isinstance(messages, (list, tuple)):
                has_tool_role = any(
                    isinstance(m, dict)
                    and isinstance(m.get("role"), str)
                    and m.get("role").strip().lower() == "tool"
                    for m in messages
                )
                if has_tool_role:
                    normalized.pop("tool_choice", None)

        # 2. Thinking flags normalization
        if not self.supports_thinking and "chat_template_kwargs" in normalized:
            ctk = normalized.get("chat_template_kwargs")
            if isinstance(ctk, dict):
                enable_thinking = ctk.get("enable_thinking")
                if enable_thinking is False or "enable_thinking" in ctk or not ctk:
                    normalized.pop("chat_template_kwargs", None)
                else:
                    ctk_copy = dict(ctk)
                    ctk_copy.pop("enable_thinking", None)
                    if not ctk_copy:
                        normalized.pop("chat_template_kwargs", None)
                    else:
                        normalized["chat_template_kwargs"] = ctk_copy
            else:
                normalized.pop("chat_template_kwargs", None)

        return normalized


# ── Built-in profiles ──────────────────────────────────────────────────

DEFAULT_PROFILE = ModelProfile(
    name="default",
    read_timeout=60.0,
    max_529_retries=1,
    backoff_529_s=1.5,
    keepalive_interval_s=15.0,
    supports_thinking=False,
)

EXACT_OVERRIDES: dict[str, ModelProfile] = {
    "nvidia/nemotron-3-ultra-550b-a55b": ModelProfile(
        name="nvidia/nemotron-3-ultra-550b-a55b",
        read_timeout=45.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=True,
    ),
    "deepseek-ai/deepseek-v4.1-flash": ModelProfile(
        name="deepseek-ai/deepseek-v4.1-flash",
        read_timeout=60.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=True,
    ),
}

NAMESPACE_PROFILES: dict[str, ModelProfile] = {
    "mistralai/*": ModelProfile(
        name="mistralai/*",
        read_timeout=90.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=False,
    ),
    "moonshotai/*": ModelProfile(
        name="moonshotai/*",
        read_timeout=180.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=False,
    ),
    "minimaxai/*": ModelProfile(
        name="minimaxai/*",
        read_timeout=60.0,
        max_529_retries=3,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=False,
    ),
    "nvidia/*": ModelProfile(
        name="nvidia/*",
        read_timeout=45.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=True,
    ),
    "google/*": ModelProfile(
        name="google/*",
        read_timeout=60.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=False,
    ),
    "stepfun-ai/*": ModelProfile(
        name="stepfun-ai/*",
        read_timeout=60.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=False,
    ),
    "z-ai/*": ModelProfile(
        name="z-ai/*",
        read_timeout=60.0,
        max_529_retries=1,
        backoff_529_s=1.5,
        keepalive_interval_s=15.0,
        supports_thinking=False,
    ),
}

WILDCARD_RULES: list[tuple[str, ModelProfile]] = [
    (
        "*kimi*",
        ModelProfile(
            name="kimi",
            read_timeout=180.0,
            max_529_retries=1,
            backoff_529_s=1.5,
            keepalive_interval_s=15.0,
            supports_thinking=False,
        ),
    ),
    (
        "*moonshot*",
        ModelProfile(
            name="moonshot",
            read_timeout=180.0,
            max_529_retries=1,
            backoff_529_s=1.5,
            keepalive_interval_s=15.0,
            supports_thinking=False,
        ),
    ),
    (
        "*nemotron*",
        ModelProfile(
            name="nemotron",
            read_timeout=45.0,
            max_529_retries=1,
            backoff_529_s=1.5,
            keepalive_interval_s=15.0,
            supports_thinking=True,
        ),
    ),
    (
        "*mistral*",
        ModelProfile(
            name="mistral",
            read_timeout=90.0,
            max_529_retries=1,
            backoff_529_s=1.5,
            keepalive_interval_s=15.0,
            supports_thinking=False,
        ),
    ),
    (
        "*minimax*",
        ModelProfile(
            name="minimax",
            read_timeout=60.0,
            max_529_retries=3,
            backoff_529_s=1.5,
            keepalive_interval_s=15.0,
            supports_thinking=False,
        ),
    ),
]


def register_exact_profile(model_id: str, profile: ModelProfile) -> None:
    """Register or override an exact model profile."""
    EXACT_OVERRIDES[model_id] = profile


def register_namespace_profile(pattern: str, profile: ModelProfile) -> None:
    """Register or override a namespace wildcard pattern profile."""
    NAMESPACE_PROFILES[pattern] = profile


def get_profile(model_id: str | None) -> ModelProfile:
    """Resolve ModelProfile with hierarchical matching:

    1. Exact model overrides (e.g. nvidia/nemotron-3-ultra-550b-a55b)
    2. Namespace wildcard match ('mistralai/*', 'moonshotai/*', etc.)
    3. Model substring/keyword heuristics (*kimi*, *nemotron*, etc.)
    4. Fallback default ModelProfile
    """
    if not model_id or not model_id.strip():
        return copy.copy(DEFAULT_PROFILE)

    mid = model_id.strip()

    # Special case: openvidia local alias maps to default
    if mid == "openvidia/openvidia":
        return copy.copy(DEFAULT_PROFILE)

    # 1. Exact model overrides
    if mid in EXACT_OVERRIDES:
        return copy.copy(EXACT_OVERRIDES[mid])
    for k, v in EXACT_OVERRIDES.items():
        if k.lower() == mid.lower():
            return copy.copy(v)

    # 2. Namespace wildcard matching
    for pattern, profile in NAMESPACE_PROFILES.items():
        if fnmatch.fnmatch(mid.lower(), pattern.lower()):
            return copy.copy(profile)

    # 3. Model keyword / wildcard rules
    for pattern, profile in WILDCARD_RULES:
        if fnmatch.fnmatch(mid.lower(), pattern.lower()):
            return copy.copy(profile)

    # 4. Fallback default
    return copy.copy(DEFAULT_PROFILE)


__all__ = [
    "DEFAULT_PROFILE",
    "EXACT_OVERRIDES",
    "ModelProfile",
    "NAMESPACE_PROFILES",
    "WILDCARD_RULES",
    "get_profile",
    "register_exact_profile",
    "register_namespace_profile",
]
