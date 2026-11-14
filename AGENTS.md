# OpenVidia — multi-key proxy for NVIDIA NIM

## Scope and architecture

OpenVidia runs a local OpenAI-compatible endpoint for remote NVIDIA-hosted
models, with optional provider failover and a desktop dashboard. It does not
provide offline inference or guarantee uninterrupted provider availability.

```text
Client → localhost:1919/v1 → proxy → NVIDIA NIM
                            ├─ chat/completions: OpenCode and OpenAI-compatible clients
                            ├─ responses: Codex adapter
                            ├─ messages: Claude Code adapter
                            └─ models: live API catalog plus official Build metadata
```

| Module | Responsibility |
|---|---|
| `proxy_app.py` | Routing, authenticated control plane, health, streaming provider failover |
| `proxy_state.py` | Key load, cooldowns, RPM limits, per-model health and circuits |
| `responses_shim.py` | Responses adapter and shared bounded key rotation |
| `anthropic_shim.py` | Messages adapter; feature support differs from native Anthropic APIs |
| `compaction.py` | Context estimation, summarization and overflow handling |
| `catalog.py` | Cached NVIDIA Build metadata intersected with live API model IDs |
| `config.py` | Configuration paths, timeouts and model options |
| `__main__.py` | CLI setup, foreground ownership, desktop view and tray |
| `server_manager.py` | Loopback IPv4/IPv6 listeners and graceful lifecycle |
| `webui.py` | Dashboard routes and persisted controls |
| `updater.py` | Managed release checks, staged installation and rollback |
| `soak.py` | Bounded operational probes and persistent reports |
| `safe_file.py` | Atomic configuration writes and backups |
| `_upstream_utils.py` | Shared upstream semaphore and worker-exhaustion detection |

`_rotation_phase()` is shared by the chat route and protocol adapters. It
separates request errors, key failures, shared-account throttling and worker
exhaustion. Gateway failures spend the bounded attempt budget; worker
exhaustion has its own retry bound. Keep timeout/rotation constants in their
existing shared modules. Never replay tool calls after output has reached a
client. Model availability, tool support and provider compatibility require
actual evidence; catalog presence alone proves none of them.

## Installation and lifecycle

```bash
./install.sh --desktop             # native view and source installation
./install.sh --headless --service  # Linux systemd user service
openvidia setup                   # persistent OpenCode and Jcode configuration
openvidia                         # attach a dashboard or start an independent proxy
openvidia foreground              # own the proxy in this terminal/service
openvidia run codex               # process-local client environment; no config edits
openvidia run claude
```

The base Python package is headless; native GUI dependencies are the
`desktop` extra (`uv sync --extra desktop` or `pip install '.[desktop]'`).
Starting the application must not rewrite client configuration or terminate
an existing listener. Closing/quitting the dashboard exits the view only;
restart is requested through the authenticated process owner.

## Development

Use English for code, comments, docstrings, documentation, commits and PRs.

```bash
uv sync --frozen
uv run pytest tests/ -q
node --test tests/dashboard.test.cjs
uv run ruff check openvidia/ tests/ scripts/
uv run ruff format --check openvidia/ tests/ scripts/
```

The test count changes with the suite; do not freeze it in contributor
instructions. Tests must isolate user configuration and use mocked transports
for upstream requests. Treat live API checks and long operational probes as
separate validation with explicit limits and reports.

The dashboard defaults to **Tested tools**: local evidence of successful streaming,
forced tool calls and tool-result round trips, expiring after six hours. Current
model failures exclude entries; historical failure ratios do not prevent recovery.
**Check tools** runs an authenticated, bounded protocol probe; the availability
button is a separate check. The OpenCode badge requires actual client evidence.
**Recent** orders live API models by official Build update dates; **All** retains
the full API catalog. Do not hardcode changing model rankings or invent API IDs
from preview cards. Build dates and successful probes are not quality benchmarks.

## Automation

CI uses the self-hosted `soliso-autcir-openvidia` runner with labels
`[self-hosted, soliso, autcir, openvidia]`, testing Python 3.12 and 3.14.
External-fork PR jobs are skipped on this persistent runner; maintainers must
review code and use an isolated environment before running untrusted changes.
The manually dispatched portable workflow defines Linux/Windows/macOS jobs;
its existence does not establish that those platforms passed.

The catalog workflow checks shipped tuning daily. An independent monitor
checks runner availability and missed scheduled runs; see
[automation monitoring](docs/automation.md). See [managed updates](docs/updates.md)
for opt-in update checks, staged installation and rollback. Do not claim that
source checkouts, published releases and installed copies update together.

## Local state

Keys and administrative tokens live under the platform configuration directory
(`~/.config/openvidia/` on Linux by default). Never commit them or include them
in reports. Important files include `keys.json`, `control_token`, `presets.json`,
`active_model`, `compaction.json`, `timeouts.json`, `model_limits.json`,
`model_options.json`, `model-validation.json`, and the optional `build-catalog.json` metadata cache.
See [security policy](SECURITY.md) before changing access controls or exposing
any endpoint beyond loopback.
