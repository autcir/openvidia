# Live model validation

A successful catalog fetch does not establish that a model can stream or call
tools. Live validation is opt-in and requires a running local OpenVidia service
with your configured NVIDIA keys. It uses real inference quota: three small
requests per selected model. It never changes the dashboard's active model.

Choose explicit API IDs from `/v1/models`; a Build playground preview alone is
not proof of public API availability. For example:

```sh
python -m openvidia.validate_models \
  --models nvidia/nemotron-3-ultra-550b-a55b minimaxai/minimax-m3.1-flash \
  --timeout 120 --concurrency 1 \
  --output /tmp/openvidia-model-validation-20261113.json
```

The validator checks local service identity, an SSE answer ending in `[DONE]`,
a valid tool invocation, and a follow-up answer consuming each tool result.
One or two model cycles can run concurrently. The timeout bounds the entire
stream/tool cycle for each model. Models are tested only when explicitly listed;
there is no automatic whole-catalog sweep.

JSON reports contain the requested model, timestamps, stage outcomes, cycle
latency, safe error categories and static contract-failure reasons. Prompts,
completions, tool arguments, raw error bodies and credentials are not recorded.
Existing report paths are refused to preserve previous results. Interrupted runs
are recorded as interrupted, with unexecuted selections marked `not_run`.

When NVIDIA supplies a `model` identifier in the buffered tool responses, the
report includes it and labels the identity `matched`, `different`, or
`not_reported`. Identity evidence is separate from protocol success: inspect a
`different` result before treating it as validation of the requested model.
Streaming response bodies are never buffered solely to collect identity data.

Exit status is zero only when every selected protocol check passed; failures
return one and a keyboard interruption returns 130. A timeout is evidence of
latency during that test window, not proof that a model is retired. A short
successful check also does not establish unattended availability or performance
on long, real repository tasks. Test those separately with OpenCode sessions and
the duration-controlled `python -m openvidia.soak` monitor.

## Dashboard checks and filtering

**Models → Tested tools** uses local checks expiring after six hours. Choose
**Recent** or **All**, then **Check tools** for a 60-second maximum cycle through
the configured local inference path. The authenticated route is
`POST /api/validate-model` with an explicit `provider/model` ID. Only one such
check runs at a time. Failed checks replace old successes; results persist in
`model-validation.json`. This action does not run OpenCode or grant its badge.
The separate availability button has a 45-second deadline and only verifies a
nonempty text completion. Neither metadata reachability nor availability alone
qualifies a model for Tested tools. The standalone CLI reports remain separate
from this local dashboard evidence.

## OpenCode integration checks

Protocol probes and actual OpenCode execution are different checks. For a
process-local custom provider, start a private server: an already-running
background server cannot inherit a new shell's `OPENCODE_CONFIG_CONTENT`.
For example, using a temporary fixture with a known expected answer:

```bash
task_fixture=$(mktemp /tmp/openvidia-read.XXXXXX)
printf 'OPENCODE_TOOL_OK\n' > "$task_fixture"
OPENCODE_CONFIG_CONTENT='{"provider":{"openvidia-check":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://127.0.0.1:1919/v1","apiKey":"ignored"},"models":{"minimaxai/minimax-m3.1-flash":{"name":"MiniMax validation","tools":true}}}}}' \
  opencode --standalone run --archived \
  --model openvidia-check/minimaxai/minimax-m3.1-flash --format json \
  "Use the read tool on the absolute path $task_fixture and reply only with its contents. Do not modify files."
rm "$task_fixture"
```

Check for a completed `read` event and the exact expected answer, not just a zero
process exit code. Keep fixture paths absolute: relative tool paths can resolve
to the active workspace. Run private servers sequentially unless their data
stores are verified to be isolated; concurrent servers sharing a SQLite data
store can fail for reasons unrelated to the model. Retain failures separately
from corrected harness runs and redact logs before sharing results.

See [the dated results](validation-results/2026-11-13.md) for the protocol and
OpenCode checks performed on 13 November 2026, including failed protocol stages.

See also [14 November results](validation-results/2026-11-14.md) for the follow-up dashboard and OpenCode alias checks.
