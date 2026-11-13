# Contributing to OpenVidia

The canonical repository is [autcir/openvidia](https://github.com/autcir/openvidia).
Fork it, create a branch and submit a pull request against `main`.

## Development setup

Python 3.12 or newer and Node.js are required for the complete local checks.
The base installation is headless; native window dependencies are optional.

```bash
uv sync --frozen
uv run pre-commit install
uv run pytest tests/ -q
node --test tests/dashboard.test.cjs
uv run ruff check openvidia/ tests/ scripts/
uv run ruff format --check openvidia/ tests/ scripts/
```

For manual desktop work, use `uv sync --frozen --extra desktop` and `uv run
openvidia`. For server operation use `uv run openvidia foreground`. Do not
run the installer merely to execute tests: it may configure local clients or
install a desktop entry/service.

`openvidia setup` configures OpenCode and Jcode. `openvidia run codex` and
`openvidia run claude` set environment variables for one process without
persistent client configuration edits. Closing the dashboard must leave an
independently running proxy available.

## Changes and tests

Use English throughout the repository and Conventional Commit subjects such
as `fix: preserve model selection when the server rejects a change`.
Ruff enforces formatting and lint rules from `pyproject.toml`. Prefer focused
functions, useful types and explanations of non-obvious behavior.

Place Python regressions in `tests/test_*.py`; dashboard behavior tests live
in `tests/dashboard.test.cjs`. Exercise outcomes and failure paths rather
than checking implementation text. Mock provider traffic and keep user
configuration isolated. Never include real credentials in fixtures or logs.

Live catalog checks are separate from the offline test suite:

```bash
uv run python -m scripts.check_catalog
```

That command contacts NVIDIA and writes `catalog-report.json`. Operational
probes can consume provider quota; use explicit duration/request limits and
record actual results. Passing a short test is not evidence of H24 uptime.

## Model discovery and tuning

`GET /v1/models` supplies selectable API IDs. `openvidia/catalog.py` annotates
only their exact intersection with official NVIDIA Build metadata; preview
cards without API access must not become selectable. The dashboard's Recent
view follows official update dates; All keeps the complete API catalog and
Answered reflects observed traffic. There is no frozen Popular model list.

Per-model options belong in `_MODEL_OPTIONS_DEFAULTS["per_model"]` in
`openvidia/config.py`. Include measurements or an official model-card source
for tuning changes. Do not infer tool compatibility or relative model quality
from an ID, model size, update date or successful catalog listing.

## CI and review

CI tests Python 3.12/3.14 on a persistent self-hosted Linux runner. For safety,
external-fork PR jobs are skipped there. A maintainer must review the changes
and arrange isolated validation; approval alone does not override that job
condition. The manual portable workflow defines additional operating-system
checks, but platform support claims need successful run evidence.

Include the problem, resulting behavior, relevant checks and remaining limits
in the PR. Update behavior documentation and the changelog when appropriate.
Keep release publication, main-branch deployment and changes to user machines
explicitly separate. See [automation](../docs/automation.md) and
[updates](../docs/updates.md) for operational details.

## Issues and security

For ordinary bugs, include the version/commit, OS, reproduction steps and
redacted error output. Use the available issue templates. Report security
issues privately using [SECURITY.md](../SECURITY.md), not public issues.

Further references: [README](../README.md), [AGENTS.md](../AGENTS.md),
[pyproject.toml](../pyproject.toml) and [source](../openvidia/).
