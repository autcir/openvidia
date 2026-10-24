# OpenVidia — Multi-key proxy for NVIDIA NIM

## Mission
Multi-key proxy for NVIDIA NIM with a native desktop dashboard. A pool of free-tier API keys behind a single endpoint, with automatic rotation, per-key cooldown, sliding-window RPM limiting, and auto-compaction.

---

## Architecture

```
Client CLI → localhost:1919/v1 → Proxy Engine → integrate.api.nvidia.com/v1
                                    │
                                    ├─ /v1/chat/completions  (catch-all: opencode, Grok, any OpenAI client)
                                    ├─ /v1/responses         (shim: Codex CLI)
                                    ├─ /v1/messages          (shim: Claude Code)
                                    └─ /v1/models            (upstream model list)
```

### Core modules

| File | Lines | Role |
|------|-------|-------|
| `proxy_app.py` | ~573 | FastAPI app factory, routing, health check, pre-warm |
| `proxy_state.py` | ~730 | Thread-safe state: KeyState, cooldown, RPM tracker, circuit breaker |
| `responses_shim.py` | ~1126 | Shim `/v1/responses` → `/v1/chat/completions` (Codex CLI) |
| `anthropic_shim.py` | ~705 | Shim `/v1/messages` → `/v1/chat/completions` (Claude Code) |
| `compaction.py` | ~784 | Context auto-compaction on context overflow |
| `config.py` | ~351 | Cross-platform config paths, timeouts, model options |
| `__main__.py` | ~727 | Entry point, CLI setup (opencode/codex/grok), tray, server manager |
| `webui.py` | ~472 | Web dashboard (pywebview), API endpoints |
| `server_manager.py` | 107 | uvicorn start/stop, dual-stack binding |
| `safe_file.py` | 197 | Atomic backup of config files |
| `_upstream_utils.py` | 43 | Global semaphore + ResourceExhausted detection |

### Shared key rotation

`_rotation_phase()` in `responses_shim.py` is the function shared by all paths:
- max 5 attempts per phase, 3 phases with a 1s pause
- saturation gate: if <5% of keys are live, fast-fail with 503
- probe timeout (90s) on the first attempt for fast dead-model detection
- send timeout (180s) on subsequent attempts
- handles 429 ResourceExhausted (transient, key untouched) vs 429 rate-limit (cooldown)
- imported by `proxy_app.py` (catch-all), `anthropic_shim.py` (Claude Code)

### Unified constants

- `_MAX_ROTATE_ATTEMPTS`, `_ROTATE_SEND_TIMEOUT`, `_MODEL_PROBE_TIMEOUT`, `_MIN_LIVE_FRACTION` → defined in `responses_shim.py`, imported everywhere
- `UPSTREAM_BASE`, `UPSTREAM_CHAT` → defined in `config.py`, imported in `anthropic_shim.py`, `compaction.py`, `proxy_app.py`, `webui.py`

---

## CLI setup

```bash
openvidia setup    # configures opencode + Codex + Grok + Jcode automatically
openvidia          # starts proxy + desktop dashboard
```

Claude Code needs manual env vars (auto-setup is disabled so it does not modify the shell rc):
```bash
export ANTHROPIC_BASE_URL=http://localhost:1919
export ANTHROPIC_API_KEY=ignored
```

---

## Development

English only: code, comments, docstrings, docs, commit messages and PRs.

```bash
pip install -e .
pytest tests/ -v          # 303 tests
ruff check openvidia/ tests/
ruff format --check openvidia/ tests/
```

### CI runner

Every workflow runs on the self-hosted runner `soliso-autcir-openvidia` on the
soliso VPS, labels `[self-hosted, soliso, autcir, openvidia]`. GitHub-hosted
runners are unavailable (the account is billing-locked). Pull requests from
outside contributors need maintainer approval before any job runs.

### Config files (`~/.config/openvidia/`)

| File | Purpose |
|------|-------|
| `keys.json` | API keys (JSON array) |
| `presets.json` | Starred models (quick-switch) |
| `active_model` | Active model (persists across restarts) |
| `index` | Key rotation index |
| `compaction.json` | Auto-compaction tuning (optional) |
| `timeouts.json` | Upstream timeouts (optional) |
| `model_limits.json` | Context windows learned by the proxy (do not edit by hand) |
| `model_options.json` | Reasoning toggle + per-model payload |
