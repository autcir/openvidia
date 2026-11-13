# OpenVidia

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow)
[![CI](https://github.com/autcir/openvidia/actions/workflows/ci.yml/badge.svg)](https://github.com/autcir/openvidia/actions/workflows/ci.yml)
![Ruff](https://img.shields.io/badge/Ruff-passed-261230?logo=ruff&logoColor=white)
![Stars](https://img.shields.io/github/stars/autcir/openvidia?style=social)
![Last Commit](https://img.shields.io/github/last-commit/autcir/openvidia)
[![Release](https://img.shields.io/github/v/release/autcir/openvidia)](https://github.com/autcir/openvidia/releases)

**Multi-key proxy for NVIDIA NIM with a native desktop dashboard.**

<p align="center">
  <img src="openvidia/web/assets/dashboard.gif" alt="OpenVidia dashboard — pooled keys, ★ Starred model shortlist, live activity log" width="400">
</p>

Pool multiple free-tier API keys behind one endpoint. Automatic rotation, per-key cooldown, sliding-window RPM limiting, and a compact desktop app — no browser needed.

Built for [opencode](https://opencode.ai), [Codex CLI](https://github.com/openai/codex), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Grok](https://x.ai), [Jcode](https://github.com/1jehuang/jcode), and any OpenAI-compatible client.

**Live evidence (13 Nov 2026):** three recent models completed real OpenCode file-tool tests; four of six API protocol cycles passed, with documented timeouts and a returned-model identity caveat. See the [dated validation report](docs/validation-results/2026-11-13.md) and [reproducible opt-in validator](docs/validation.md). The 72-hour run is still in progress.

---

## Quick Start

### Linux (Arch / Ubuntu / Fedora)

```bash
git clone https://github.com/autcir/openvidia.git
cd openvidia
./install.sh
```

Or manually:

```bash
# Install uv (recommended) — https://astral.sh/uv
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync --frozen --extra desktop  # install desktop dependencies
uv run --extra desktop openvidia setup  # configure opencode
uv run --extra desktop openvidia        # reuse/start proxy + desktop app
```

With pip:

```bash
pip install -e '.[desktop]'
openvidia setup
openvidia
```

### macOS

```bash
brew install python@3.12
git clone https://github.com/autcir/openvidia.git
cd openvidia
pip install -e '.[desktop]'
openvidia setup
openvidia
```

> pywebview on macOS uses system WebKit (native, no extra deps).
> GTK/PyGObject is not required for the native macOS backend.

### Windows

```cmd
git clone https://github.com/autcir/openvidia.git
cd openvidia
pip install -e ".[desktop]"
openvidia setup
openvidia
```

> pywebview on Windows uses EdgeChromium (WebView2, pre-installed on Windows 10/11).
> If WebView2 is missing, install it from [Microsoft](https://developer.microsoft.com/en-us/microsoft-edge/webview2/).

---

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                     OpenVidia (:1919)                       │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │               Desktop App (pywebview)               │    │
│  │  310×570 native window — Keys, Presets, Models,     │    │
│  │  Activity log, CLI setup — all in one panel         │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │               Proxy Engine (:1919/v1)               │    │
│  │                                                     │    │
│  │  Request → override model → pick key → forward      │    │
│  │            ↑                ↑           ↑           │    │
│  │            │            cooldown?   RPM < 28?       │    │
│  │            │            skip if yes  skip if no     │    │
│  │            │                                        │    │
│  │  On 429: read Retry-After → set cooldown → next key │    │
│  │  On 401/403: cooldown 3600s (dead key)              │    │
│  │  On 400/404: cooldown 60s (deterministic)           │    │
│  │  On 504: cooldown 30s (model bottleneck, not key)   │    │
│  │  On 5xx: cooldown 10s (gateway) / 30s (other)       │    │
│  │  On network error: no cooldown, 0.5s pause          │    │
│  │  3 consecutive network errors → stop (network down) │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│                   NVIDIA NIM API                            │
│            integrate.api.nvidia.com/v1                      │
└─────────────────────────────────────────────────────────────┘
```

---

## Why?

NVIDIA rate limits and shared worker availability vary by model and account. Bursts can trigger cooldowns; adding keys cannot bypass a shared account limit. OpenVidia:

- **Pools multiple keys** behind a single endpoint
- **Rotates automatically** on eligible key/provider errors, with bounded attempts and explicit failure when the pool cannot serve the request
- **Per-key cooldown timers** — on real 429s, trusts `Retry-After` exactly (no multiplication); distinguishes transient worker saturation (`ResourceExhausted`) from true RPM limits and never burns key cooldowns for the former
- **Network-error circuit breaker** — `ConnectTimeout`/`ConnectError` don't trigger key cooldown (the key is fine). After 3 consecutive network errors across different keys, rotation stops entirely and returns 503 (the network is down, no key can help)
- **Bounded gateway retries** — gateway failures cool the key for 30s and spend the attempt budget so a large pool cannot amplify an outage indefinitely
- **Sliding-window RPM limiting** — starts with a 28 RPM per-key ceiling and adapts downward from `Retry-After`; account-level throttling still takes precedence
- **Health checks** — revives keys whose cooldowns have expired, but skips 401/403 invalid keys (dead keys stay dead)
- **Explicit routing policy** — the requested model is forwarded to NVIDIA; model/provider failover requires explicit configuration. Provider-reported model identity can differ and is recorded separately by the live validator.
- **Auto-compaction** — summarizes or trims long histories to reduce context-overflow failures ([details](#auto-compaction))
- **Per-key × per-model scoring** — rotation is ordered by a composite score (success rate + median time to first token) learned per key and per model from live traffic: the best key for the requested model goes first
- **Multi-endpoint routing** — extra NVIDIA endpoints (`endpoints.json` or `OPENVIDIA_UPSTREAM_ENDPOINTS`), a 60s blacklist on eligible endpoint errors (model-specific 504 timeouts do not blacklist the shared host), automatic recovery
- **Multi-node sync via Redis (opt-in)** — cooldowns, invalid keys, pool throttling and model circuit breakers shared across instances ([details](#resilience--scale))
- **Embedding cache** — `/v1/embeddings` hits are served from memory (TTL 300s) without touching upstream quota
- **Free-tier provider fallback** — when every NVIDIA key is exhausted, requests fail over to OpenAI-compatible providers declared in `providers.json` ([details](#resilience--scale))
- **Graph engine** — programmatic multi-agent orchestration (Hub, run_agent, generator-verifier loops) on top of the proxy ([details](#graph-engine))

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `openvidia` | Start proxy in background + open desktop app |
| `openvidia foreground` | Foreground mode (logs to stdout, no UI) |
| `openvidia setup` | Configure opencode and Jcode; other clients use `openvidia run <cli>` |

---

## CLI Setup Guides

> **TL;DR — run `openvidia setup` once.** It configures opencode and Jcode. Use `openvidia run <cli>` for process-scoped configuration of other clients. You only need to read the manual steps below if something doesn't work or you prefer to configure things yourself.

Supported clients:

| CLI | Protocol | Endpoint | Auto-setup |
|-----|----------|----------|------------|
| **opencode** | OpenAI-compatible | `http://localhost:1919/v1` | ✅ `openvidia setup` |
| **Codex CLI** | OpenAI Responses API | `http://localhost:1919/v1` | `openvidia run codex` |
| **Claude Code** | Anthropic Messages API | `http://localhost:1919` | ❌ manual env vars |
| **Grok (xAI)** | OpenAI-compatible | `http://localhost:1919/v1` | `openvidia run grok` |
| **Jcode** | OpenAI-compatible | `http://localhost:1919/v1` | ✅ `openvidia setup` |

---

### opencode

```bash
openvidia setup    # configures provider + model + default compaction
opencode           # then /model openvidia
```

Manual (if `setup` didn't find it):

```bash
# ~/.config/opencode/opencode.json
{
  "provider": {
    "openvidia": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "apiKey": "ignored", "baseURL": "http://localhost:1919/v1" },
      "models": { "openvidia": { "name": "OpenVidia", "tools": true } }
    }
  },
  "model": "openvidia/openvidia"
}
```

---

### Codex CLI

```bash
openvidia setup    # writes ~/.codex/config.toml automatically
codex --model openvidia
```

Manual (if `setup` didn't find it):

```toml
# ~/.codex/config.toml
model = "openvidia"
model_provider = "openvidia"

[model_providers.openvidia]
name = "OpenVidia"
base_url = "http://localhost:1919/v1"
env_key = "OPENVIDIA_API_KEY"
wire_api = "responses"
```

```bash
export OPENVIDIA_API_KEY=ignored   # also added automatically by setup
```

---

### Claude Code

```bash
# Add to ~/.zshrc or ~/.bashrc manually (auto-setup is disabled)
export ANTHROPIC_BASE_URL=http://localhost:1919
export ANTHROPIC_API_KEY=ignored
```

```bash
source ~/.zshrc    # (or restart your terminal)
claude --model openvidia
```

> Auto-setup for Claude Code is intentionally disabled to avoid mutating shell rc files. Set the two environment variables manually as shown above.

> The `/v1/messages` endpoint translates Anthropic Messages format ↔ NVIDIA chat/completions bidirectionally — streaming, tool use, and system prompts all work.
>
> **Images:** This Anthropic compatibility shim currently replaces image blocks with text placeholders and logs a warning. NVIDIA also serves vision models; this shim limitation must not be interpreted as a provider-wide lack of vision support.

---

### Grok (xAI)

```bash
openvidia setup    # writes ~/.grok/config.toml automatically
grok -m openvidia
```

Manual (if `setup` didn't find `~/.grok/`):

```toml
# ~/.grok/config.toml
[models]
default = "openvidia"

[model.openvidia]
api_key = "ignored"
base_url = "http://localhost:1919/v1"
api_backend = "chat_completions"
context_window = 128000
```

---

### Jcode

```bash
openvidia setup    # writes ~/.jcode/config.toml automatically
jcode               # then /model openvidia to switch
```

Manual (if `setup` didn't find `~/.jcode/`):

```toml
# ~/.jcode/config.toml

[provider]
default_provider = "openvidia"
default_model = "openvidia"

[providers.openvidia]
type = "openai-compatible"
base_url = "http://localhost:1919/v1"
auth = "none"
default_model = "openvidia"
requires_api_key = false
model_catalog = true
stream_idle_timeout_secs = 300

[[providers.openvidia.models]]
id = "openvidia"
context_window = 202752
```

> `openvidia setup` adds the `[providers.openvidia]` block and sets it as default — but only when no provider is configured yet. If you already use another provider, it leaves your choice alone and you can switch with `/model openvidia` inside Jcode.
>
> Jcode is an open-source coding agent: [github.com/1jehuang/jcode](https://github.com/1jehuang/jcode)

---

### Any OpenAI-compatible client

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:1919/v1", api_key="ignored")
response = client.chat.completions.create(
    model="openvidia",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

```bash
curl http://localhost:1919/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ignored" \
  -d '{"model":"openvidia","messages":[{"role":"user","content":"Hello!"}]}'
```

Streaming (SSE) responses flow incrementally; model and client protocol capabilities must still be tested.

---

## Smart Rate Limiting

### Per-Key Cooldown

| HTTP Status | Cooldown | Reason |
|-------------|----------|---------|
| **429** (real RPM limit) | `Retry-After` header (trusted as-is, no multiplication), or 45s + jitter | Rate limited — honour NVIDIA's own window exactly |
| **429** (worker concurrency) | 0 — retried after 0.8s pause | `ResourceExhausted: Worker local total request limit reached` — transient pool saturation, key is healthy |
| **401 / 403** | 3600s | Dead key — don't waste requests |
| **400 / 404** | 60s | Deterministic request error — short cooldown, key untouched for rotation. Every key gets the same error, so rotating only burns cooldown budget. |
| **504** (gateway timeout) | 30s | Model is the bottleneck, not the key — longer cooldown, but doesn't burn the attempt budget |
| **5xx** (other) | 10s (502/503) / 30s (500) | Server error — retry soon |
| **Network error** (ConnectTimeout/ConnectError) | 0 — 0.5s pause, no cooldown | Network is down, not the key. After 3 consecutive errors across different keys, rotation stops entirely (503). |

> **Retry-After is used as-is.** When NVIDIA provides a `Retry-After` header (e.g. 60s), the cooldown is exactly that value. Earlier versions multiplied it by an adaptive factor (`1.5^N`), which caused a doom loop: at 3 failures a 60s backoff became 135s and all 26 keys locked out longer than the real rate-limit window required.
>
> When no `Retry-After` is provided the proxy uses 45s + random jitter (≤10s). Repeated failures get a flat 1.5× multiplier from the second failure onward (no exponential scaling — capped at ~65s max).

### Sliding-Window RPM

Each key tracks requests in a rolling 60-second window. If a key has sent **28+ requests** in the last 60s, it is skipped. Only if all keys are simultaneously RPM-saturated or on cooldown does the proxy return 429 to the client.

### Key Rotation Flow

```
Request arrives
    │
    ├─ For each candidate key (ordered least-loaded first):
    │   ├─ Key on cooldown?  → skip (including keys cooled mid-pass)
    │   ├─ Key RPM ≥ 28?    → skip
    │   ├─ Send to NVIDIA   → 200? ✅ record RPM, return response
    │   │                   → 400/404? set 60s cooldown, next key
    │   │                   → 429 ResourceExhausted? pause 0.8s, retry (key untouched)
    │   │                   → 429 rate-limit? use Retry-After as-is, set cooldown, next key
    │   │                   → 401/403? set 3600s cooldown, next key
    │   │                   → 504? set 30s cooldown, next key (doesn't burn attempt budget)
    │   │                   → 5xx? set 10s (502/503) or 30s (500), next key
    │   │                   → network error? 0.5s pause, key untouched (3 consecutive → stop)
    │   └─ (max 5 sends per pass, 3 passes with 1s pause between)
    │
    └─ All candidates exhausted? → an explicit error, or a separately configured provider fallback
```

### Health Check

Every 60 seconds:
1. Finds keys still on cooldown (skips 401/403 invalid keys and unexpired 429 rate-limit cooldowns)
2. Sends a lightweight `GET /v1/models` probe
3. If the key responds OK — clears the cooldown (revived)
4. If still failing — leaves the cooldown in place

---

## Auto-Compaction

Long conversations eventually exceed the model's context window. Without handling, the upstream returns a `400`, and — since that error is identical on every key — a naive proxy would burn through the whole pool before dying. **Compaction reduces overflow risk; provider limits can still reject a request.**

Before forwarding a request, if the estimated history exceeds a token budget, OpenVidia compacts it:

```
history > budget?
    │
    ├─ Cached summary still fits?
    │      └─ Serve summary + EVERY later message verbatim.  ← steady state,
    │         zero upstream calls. The summary boundary only moves when this
    │         no longer fits, so you pay for a summarize once every N turns.
    │
    ├─ Boundary must advance → summarize the oldest slice on top of the
    │      previous summary (incremental — never the whole history again).
    │      The verbatim tail is sized to FILL the remaining budget.
    │
    └─ Summary not ready before `inline_deadline`?
           └─ Serve now (cached summary + verbatim remainder, or a
              deterministic trim) while the summarize keeps running
              detached and lands in the cache for the next turn.
```

- **Never blocks the client** — the request waits at most `inline_deadline` seconds, regardless of how slow the upstream is. Compaction latency is bounded by config, not by the provider.
- **Works for every client** — hooks `/v1/chat/completions` (opencode / Codex), `/v1/responses`, and the `/v1/messages` Anthropic shim (Claude Code).
- **Cheap** — the steady state costs zero extra calls; only genuinely new content is ever summarized, and concurrent requests on the same conversation share a single summarize.
- **Safe** — every fallback is bounded, so a request goes through even with the whole key pool rate-limited.

Watch it in the **Activity** log: `⧉ compaction: summarized N new msg (covers M) → …`.

### Tuning

> **Set `model_budgets` first.** The generic default (80k) is deliberately conservative because NVIDIA NIM does not advertise a context window on `/v1/models`. If your model accepts more, compaction at 80k throws away context you paid nothing for. To find the real number, send an oversized request — the `400` states it exactly:
>
> ```
> This model's maximum context length is 202752 tokens. However, your
> messages resulted in 320011 tokens.
> ```

Optional — create `~/.config/openvidia/compaction.json` (built-in defaults shown):

```json
{
  "enabled": true,
  "budget_tokens": 80000,
  "model_budgets": { "your-provider/your-model": 160000 },
  "reserved_tokens": 8000,
  "compact_ratio": 0.6,
  "keep_recent": 8,
  "summary_model": "",
  "summary_max_tokens": 1024,
  "inline_deadline": 6.0,
  "summarize_timeout": 45.0
}
```

| Field | Meaning |
|-------|---------|
| `enabled` | Turn compaction on/off |
| `budget_tokens` | Generic trigger for any model without an explicit budget |
| `model_budgets` | Per-model context window. **The one setting worth tuning** — leave ~20% headroom, the estimator is a ~4 chars/token approximation |
| `reserved_tokens` | Generation headroom subtracted from the budget |
| `compact_ratio` | Compact down to this fraction of the budget. Landing exactly on the threshold re-triggers compaction on the very next turn |
| `keep_recent` | Floor for the trim fallback. On the summary path the verbatim tail is sized to fill the budget instead |
| `summary_model` | Model used to summarize. Point it at a **fast** model: it runs on a different traffic stream than the one your agent is saturating. Empty = the default model |
| `summary_max_tokens` | Cap on the generated summary length |
| `inline_deadline` | Seconds the *client request* will wait for a summary before being served from the fallback ladder |
| `summarize_timeout` | Upstream cap for the summarize call itself, which continues in the background past the deadline |

---

## Desktop App

Native window via [pywebview](https://pywebview.flowrl.com/). Opens at **310×570 px** — a compact utility panel, like a phone in portrait. Resize freely.

| Backend | Platform | Engine |
|---------|----------|--------|
| **Qt WebEngine** | Linux (KDE/Wayland) | PyQt6-WebEngine (native, best experience) |
| **GTK WebKit** | Linux (GNOME/X11) | PyGObject + WebKitGTK |
| **WebKit** | macOS | system WebKit (no extra deps) |
| **EdgeChromium** | Windows | WebView2 (pre-installed on Win 10/11) |

pywebview auto-detects the best available backend.

### Linux desktop integration

```bash
# .desktop file (auto-installed by install.sh)
cp openvidia.desktop ~/.local/share/applications/
# Icon
cp openvidia/web/assets/logo.png ~/.local/share/icons/hicolor/256x256/apps/openvidia.png
update-desktop-database ~/.local/share/applications/
```

---

## Dashboard Sections

| Section | Features |
|---------|----------|
| **Status** | Proxy state, active model, start/stop/restart controls |
| **Stats** | Request count, success rate, rotations, cooldown counter |
| **Keys** | Per-key status (Active filter default), live cooldown countdown, RPM, success/fail, freshness dots, add/remove/copy |
| **Models** | Single list — filters: **★ Starred** (default; your quick-switch shortlist) · All · Popular. Search, test ▶, star/unstar. Active model highlighted and pinned to top. |
| **Thinking** | `auto` / `on` / `off` next to the active model — a hybrid reasoning model emits nothing while it thinks |
| **Activity** | Real-time SSE log stream with color-coded levels |
| **CLI Setup** | Copy-paste config for opencode / Codex / Claude / Grok |

### Key Status Indicators

| Indicator | Meaning |
|-----------|---------|
| 🟢 Green | Key healthy, has successful requests |
| 🟡 Amber | Key has failures but not on cooldown |
| ⚪ Gray | Key idle (no requests yet) |
| 🔴 Red + ⏳ | Key on cooldown — shows countdown + reason |
| `active` badge | Currently selected key in rotation |

---

## Configuration

### Config directory

| Platform | Path |
|----------|------|
| **Linux** | `~/.config/openvidia/` |
| **macOS** | `~/Library/Application Support/openvidia/` |
| **Windows** | `%APPDATA%\openvidia\` |

### Config files

| File | Purpose |
|------|---------|
| `keys.json` | API keys (JSON array) |
| `presets.json` | ★ Starred models — quick-switch shortlist |
| `active_model` | Currently active model (persists across restarts) |
| `index` | Key rotation index |
| `compaction.json` | Auto-compaction tuning (optional — see [Auto-Compaction](#auto-compaction)) |
| `timeouts.json` | Upstream timeouts (optional — see [Slow models](#slow-models)) |
| `model_limits.json` | Context windows the proxy learned by itself — never edit by hand |
| `model_options.json` | Reasoning toggle + the payload used to express it (see [Thinking](#thinking-reasoning-toggle)) |
| `endpoints.json` | Extra NVIDIA upstream endpoints (optional — see [Resilience & Scale](#resilience--scale)) |
| `redis_config.json` | Redis URL for multi-node sync (optional — `OPENVIDIA_REDIS_URL` also works) |
| `providers.json` | Free-tier fallback providers (optional — name, base URL, env var for the key, model map) |

Add keys via the dashboard (**Keys** section) or edit `keys.json`:

```json
["nvapi-xxx", "nvapi-yyy", "..."]
```

### Thinking (reasoning toggle)

A hybrid reasoning model emits **nothing at all** while it thinks. That is the
whole difference between a 2-second and a 160-second first token, and it is
not something you can see from the outside — the socket just sits there.

Three buttons next to the active model in the dashboard: `auto` (send nothing,
let the model decide), `on`, `off`. The setting is per-model and stored
server-side, so supported request paths apply it without editing client configs.

The **parameter name is configuration, not code**. Providers spell this flag
differently and rename it every model generation, so `model_options.json`
carries the payload to merge:

```json
{
  "thinking": "auto",
  "thinking_off_payload": { "chat_template_kwargs": { "enable_thinking": false } },
  "thinking_on_payload":  { "chat_template_kwargs": { "enable_thinking": true } },
  "per_model": { "vendor/model": { "thinking": "off" } }
}
```

A future model that wants `{"reasoning_effort": "none"}` instead needs an edit
here, not a release. The merge only fills what the client did not set, at
every level of nesting: a CLI that spells the parameter out in its own request
has made an explicit choice and wins.

---

### Context windows are learned, not configured

New models use a conservative context budget until an explicit override or a
provider overflow rejection supplies a learned limit. This avoids relying on a
hand-maintained table, but it does not establish the full context capacity in
advance.

NVIDIA does not advertise the window on `/v1/models`, but it states it exactly
when a request exceeds it:

```
This model's maximum context length is 202752 tokens.
However, your messages resulted in 320011 tokens.
```

The proxy learns the limit from a real overflow rejection and caches the answer in
`model_limits.json`. It does not send oversized speculative probes. Precedence:
your `model_budgets` override → learned → the conservative default. An unknown
model uses the conservative default until the provider supplies a learned limit.

New models appear in the live catalog. Their actual context and tool compatibility
must be learned or tested; listing a model does not certify its capabilities.

---

### No pinned model

There is no `DEFAULT_MODEL` constant. The model a request runs on is resolved
live: your active selection, then the first starred preset, otherwise an error
saying no model is selected. A hardcoded model name is a liability the day the
provider retires it, and it silently overrides what you picked.

---

### Slow models

A `read` timeout is the wait for the **first byte**, and a reasoning model
emits nothing at all while it thinks. Measured on the NVIDIA free tier in
July 2026, the same key, within the same minute (these models have since been
retired; the pattern still holds for their successors):

| Model | Time to first token |
|-------|---------------------|
| `deepseek-ai/deepseek-v4-flash` | 2.1s |
| `deepseek-ai/deepseek-v4-pro` | 12.3s |
| `minimaxai/minimax-m3` | 44.5s |
| `z-ai/glm-5.2` | 2–4s (thinking=off) / 162s (thinking=on) |

Provider capacity for one model can collapse without warning while the others
stay fast — so a timeout short enough to feel responsive is also short enough
to make a slow model **fail on every key in the pool**. OpenVidia therefore
waits (default 240s) and keeps the SSE stream alive with periodic comments, so
your CLI can tell "thinking" from "dead". A read timeout never puts a key on
cooldown: the key connected fine and the upstream accepted the request — it is
the model that is slow, and cooling keys down for it drains the whole pool.

Override in `~/.config/openvidia/timeouts.json`:

```json
{ "connect": 5.0, "read": 240.0, "write": 30.0, "pool": 240.0 }
```

If a model is too slow to work with, switch model in the dashboard. OpenVidia
will not silently answer from a different one.

### Rate limit tuning

Constants in `openvidia/proxy_state.py`:

```python
MAX_RPM = 28              # Initial local ceiling; provider quotas can be lower
RPM_WINDOW = 60.0         # Sliding window in seconds

COOLDOWN_DURATIONS = {
    400: 60.0,            # Deterministic client error — key untouched
    401: 3600.0,          # Unauthorized — dead key
    403: 3600.0,          # Forbidden — dead key
    404: 60.0,            # Endpoint issue — short cooldown
    429: 45.0,            # Rate limited (Retry-After overrides when provided)
}
# 400/404 are deterministic content errors: the key is left untouched,
# so rotating on them would only burn cooldown budget.
DEFAULT_COOLDOWN = 30.0   # Network errors, unknown 5xx
```

---

## API Endpoints

### Proxy

| Method | Path | Description |
|--------|------|-------------|
| `*` | `/v1/{path}` | Forward to NVIDIA NIM (streaming supported) |
| `POST` | `/v1/responses` | OpenAI Responses API shim (Codex CLI) |
| `POST` | `/v1/messages` | Anthropic Messages API shim (Claude Code) |
| `GET` | `/v1/models` | List available models from upstream |
| `GET` | `/health` | Health check — key count, port, status |

### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Proxy running state + cooldown count |
| `GET` | `/api/stats` | Requests, rotations, success, cooldowns, total RPM |
| `GET` | `/api/keys/stats` | Per-key: requests, success/fail, cooldown, RPM, reason |
| `GET` | `/api/keys` | List keys |
| `POST` | `/api/keys` | Replace all keys |
| `POST` | `/api/keys/add` | Add a key |
| `POST` | `/api/keys/remove` | Remove a key |
| `GET/POST` | `/api/model` | Get/set active model override |
| `GET/POST` | `/api/presets` | Get/save model presets |
| `GET/POST` | `/api/thinking` | Get/set the reasoning mode of the active model (`auto` / `on` / `off`) |
| `GET` | `/api/model-health` | What the proxy learned from live traffic: per-model success rate, median time to first token, gateway timeouts, 429s |
| `POST` | `/api/test-model` | Test a model directly (bypasses override) |
| `POST` | `/api/stop` | Stop proxy (returns 503 to clients) |
| `POST` | `/api/start` | Resume proxy |
| `POST` | `/api/restart` | Zero-downtime restart (spawn new, kill old) |
| `GET` | `/api/logs/stream` | SSE log stream (real-time) |

### Ops (control plane — token protected)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/ops/keys` | Per-key status: cooldown, RPM + learned ceiling, per-model score, in-flight |
| `GET` | `/ops/health` | Pool, per-model health, endpoints, embedding cache, Redis sync, recent logs |

---

## Graph Engine

Programmatic multi-agent orchestration on top of the proxy, following the
[graph-engine coordination patterns](https://www.anthropic.com/engineering/graph-engine-coordination):
a single `Hub` owns mailboxes and tasks, agents run with hard caps, and workers
can be spawned, messaged and killed from inside the loop. Runs on any model the
proxy serves.

```python
import asyncio
from openvidia import Hub, Provider, ToolSpec, run_agent

async def add(args, ctx):
    return str(args["a"] + args["b"])

async def main():
    hub = Hub(default_provider=Provider())  # http://127.0.0.1:1919/v1 — the proxy above
    try:
        result = await run_agent(
            hub, "math",
            system="You are a calculator. Always use the add tool.",
            first_turn="What is 5 + 2?",
            tools=[ToolSpec(
                name="add", description="Sum two integers",
                handler=add,
                parameters={"type": "object", "properties": {
                    "a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"]},
            )],
        )
        print(result)
    finally:
        await hub.close()
```

Highlights: `run_agent` caps every run with `max_iterations`, `budget_tokens`
and `timeout_s`; `graph_tools(hub, worker_factory=...)` provides
`get_status`, `send_message`, `wait_for_message`, `create_subagents` (depth-bound:
workers cannot spawn workers) and `kill_subagents`; `generate_and_verify` runs
generator → verifier loops with explicit rubrics until `APPROVED` or
`max_rounds`; providers are swappable per worker (a `Provider` or a
`Callable[[str], Provider]`). 10 regression tests ship in `tests/test_graph_engine.py`.

---

## Resilience & Scale

Beyond the core pool, OpenVidia is built to survive upstream trouble and to
scale across machines.

- **Multi-endpoint routing** — by default requests go to
  `integrate.api.nvidia.com`; add endpoints via `endpoints.json` or
  `OPENVIDIA_UPSTREAM_ENDPOINTS` (comma-separated). Eligible endpoint failures blacklist an
  endpoint for 60s; model-specific 504 latency does not. A success revives it. Deterministic client errors never
  retry on another endpoint.
- **Multi-node sync (Redis, opt-in)** — one proxy per machine, shared state:
  cooldowns, invalid keys, pool throttling and model circuit breakers propagate
  over pub/sub so a 429 learned on node A protects node B. Enable with
  `OPENVIDIA_REDIS_URL` or `redis_config.json` (`{"url": "..."}`); install the
  extra with `pip install "openvidia[redis]"`.
- **Embedding cache** — repeated `/v1/embeddings` requests (same model + input)
  are answered from an in-memory cache (TTL 300s) keyed by SHA-256; hit/miss
  counters are exposed on `/ops/health`.
- **Free-tier provider fallback** — when every NVIDIA key is exhausted, the
  catch-all fails over to OpenAI-compatible providers listed in
  `providers.json`:

  ```json
  [
    {
      "name": "glm",
      "base_url": "https://api.example.com/v1",
      "api_key_env": "GLM_API_KEY",
      "models": { "deepseek-ai/deepseek-v4-flash": "glm-5" }
    }
  ]
  ```

  A provider without its `api_key_env` set in the environment is skipped
  silently; model names are rewritten through the `models` map.

---

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com/)** — async web framework
- **[httpx](https://www.python-httpx.org/)** — HTTP/2 client for upstream
- **[uvicorn](https://www.uvicorn.org/)** — ASGI server
- **[pywebview](https://pywebview.flowrl.com/)** — native desktop window (Qt/GTK/WebKit/EdgeChromium)
- **[psutil](https://github.com/giampaolo/psutil)** — cross-platform process management
- **[tomlkit](https://github.com/sdispater/tomlkit)** — TOML config editing (preserves comments)
- **Vanilla HTML/CSS/JS** — zero frontend build, no node_modules
- **Python 3.12+** — single process, no external services
- **[redis](https://redis.io/)** (optional) — multi-node state sync via `redis-py`

---

## License

MIT

---

Built by [Ciro Autuori](https://github.com/autcir).


## Reliable operation and updates

For an unattended Linux service, install with `./install.sh --headless --service`.
For a desktop plus managed service, use `./install.sh --desktop --service`.
The desktop reuses an existing OpenVidia process. Clicking its tray icon toggles
the dashboard; the tray menu also provides Show and Hide dashboard. Restoring a
minimized window returns it to its normal state. Closing the window or choosing
Close dashboard exits or hides the view only; it does not terminate the proxy. An unrelated
process on the configured port is never terminated automatically.

The model browser refreshes the live catalog every 60 seconds and keeps its last
successful catalog during network failures, with visible freshness/error status.
**Answered** means a model has successfully responded in this proxy process;
it does not certify tool use, vision or a context limit. Retired favorites remain
visible as unavailable. Changing a model or reasoning setting is confirmed by
the authenticated server before the interface reports success.

`/health` identifies the running proxy independently of desktop assets. `/ready`
reports whether it is enabled and has an eligible key; it is
not a successful-inference guarantee. Real streaming and tool roundtrips are
checked by the long-duration monitor in `python -m openvidia.soak --help`.

Application updates are separate from catalog updates. See
[verified release installation and rollback](docs/updates.md). Checks are opt-in;
updates do not replace dependencies inside a running environment or restart an
active conversation. Provider fallback is also opt-in via `providers.json` and
currently applies to Chat Completions (OpenCode), including incremental SSE and
tool messages. Responses/Messages protocol shims do not promise cross-provider
failover. A partially emitted response is never replayed on another provider.

The automated Linux tests and local smoke checks do not establish 24/7 uptime or
Windows/macOS support on every machine. The manual Portable compatibility
workflow runs the suite and resolves desktop dependencies on Linux, Windows and
macOS when GitHub-hosted runners are available. NVIDIA quotas, shared worker
availability and the selected model remain external dependencies; adding keys
cannot guarantee unlimited throughput or continuous free service.


### Recent models in the desktop picker

The default **Recent** view matches the latest NVIDIA Build page against IDs
actually returned by the inference API, then sorts by the official catalog
modification date. It shows official display names and use-case tags while
keeping the exact API ID searchable. This is recency metadata, not a benchmark
ranking or a claim that a larger/newer model is better at your task.

The complete inference catalog, including older and specialized models, remains
under **All**. Build metadata is cached for one hour with a bounded fetch and
last-good stale fallback. If the optional metadata cannot be loaded, the picker
clearly points to All. Its scope is the latest Build page, not every model
NVIDIA has ever published. Preview/playground-only names absent from the API are
never added as selectable models merely because a website labels them Free
Endpoint. Catalog metadata refresh never changes your active model.
