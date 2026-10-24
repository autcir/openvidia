# wf-002 — graph_engine public API

Label: `wayfinder:task` · AFK · **CLOSED**

blocked-by: wf-001

## Question

Which public API does `openvidia/graph_engine.py` expose? Design already decided in session: `Hub` (register/new_name/close/snapshot), `run_agent`, `generate_and_verify`, `ToolSpec`/`WorkerSpec`/`Provider` dataclasses, `graph_tools` (get_status, send_message, wait_for_message, create_subagents, kill_subagents), exported from `__init__.py`.

## Resolution

Implemented and verified (10/10 tests, clean ruff): `openvidia/graph_engine.py` with `Provider` (async OpenAI-style chat), `ToolSpec`, `WorkerSpec`, `Hub` (status/mailbox/task/events), `run_agent` (loop with max_iterations, budget_tokens, timeout, crash on empty response, tool error recovery), `graph_tools` (closure over the factory for spawn/kill, depth bound: workers do not spawn), `generate_and_verify` (explicit rubric, APPROVED/REJECTED, feedback carried into the next round). Public exports from `openvidia/__init__.py`. Tests: `tests/test_graph_engine.py` (10 tests, ScriptedProvider).
