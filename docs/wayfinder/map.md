# Graph Engine: wayfinder map

Label: `wayfinder:map` — local-markdown tracker (GitHub issues disabled on the repo).

## Destination

Module `openvidia/graph_engine.py` (~400 lines, async) that implements the Graph Loop on local DeepSeek (localhost:1919): Hub + run_agent + spawn/status/collect/kill tools + generate_and_verify, with passing pytest tests and clean ruff.

## Notes

- domain: python async, httpx, agent orchestration (Anthropic patterns: orchestrator-subagent, generator-verifier, fresh context, depth bound)
- skill: /grilling, /domain-modeling, /tdd
- OVERRIDE: effort in **full-delivery** — tickets are resolved within the same session, not just decided. Minimal chart, then full implementation.

## Decisions so far

- [wf-001 full-delivery approved](tickets/wf-001-full-delivery.md) — the user asked for map + full implementation in the same session ("E POI PARTI FULL DELIVERY", i.e. "and then go full delivery").
- [wf-002 graph_engine public API](tickets/wf-002-public-graph-engine-api.md) — `openvidia/graph_engine.py` implemented and verified: Hub + run_agent + graph_tools + generate_and_verify, 10/10 tests, clean ruff, exported from `__init__.py`.

## Not yet specified

- Alternative (non-DeepSeek) providers via env/CLI
- Persisting graph sessions to disk

## Out of scope

- Multi-node Redis / upstream HTTP2 pool (separate proxy roadmap)
- Graph engine dashboard frontend
