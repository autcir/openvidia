# Independent automation monitoring

A scheduled workflow cannot reliably detect that its own runner is offline.
Run `scripts/check_automation.py` from a separate machine, such as the desktop,
to detect stopped GitHub automation even when no Actions job starts.

Requirements: Python 3.12 or newer and GitHub CLI (`gh`), authenticated as an
account allowed to read this repository's Actions runners and workflow runs.
Use the existing `gh auth login` credential store. The script never reads or
prints tokens, forwards API error output, or sends messages, issues or email.

```bash
python scripts/check_automation.py \
  --repository autcir/openvidia \
  --output "$HOME/.local/state/openvidia/automation.json"
```

The output path is required. Every invocation atomically replaces the private
JSON report, including on GitHub API/authentication errors. Exit code `0` means
all checks passed; `1` means unhealthy or the report could not be written.

The report contains the check time, repository default branch, required runner
state, recent catalog execution summaries, last successful completion age,
expected scheduled execution and explicit failure reasons. It does not store
raw GitHub responses, credentials, run inputs or API error output.

The monitor checks:

- The `soliso-autcir-openvidia` repository runner is online. Override its name
  with `--runner` if deployment changes.
- The latest completed `catalog.yml` run on the default branch succeeded.
  Pull request and feature-branch runs cannot satisfy this check.
- A successful catalog completion exists within 48 hours. Change this with
  `--max-age-hours` when an intentional operational policy requires it.
- The daily 06:17 UTC scheduled execution completed successfully after a
  two-hour allowance for scheduling and queue delays. Change the allowance
  with `--schedule-grace-hours`. A recent manual run cannot mask a missing
  scheduled run. Update the monitor's expected schedule together with the
  workflow if its cron expression changes.

For hourly checks on Linux, install a user systemd oneshot service invoking
this command with absolute executable/script/report paths, plus a timer with
`OnCalendar=hourly` and `Persistent=true`. Keep this timer on a host independent
of the GitHub runner. Inspect the JSON report and `journalctl --user` to find
failures; the monitor does not create external notifications.

This check observes automation freshness, not proxy availability, model
quality or a guaranteed GitHub/NVIDIA service level. A sleeping or offline
monitor host also cannot perform checks until it resumes. Continuous coverage
requires an always-on independent host.


Self-hosted CI accepts repository pushes and same-repository pull requests.
Fork pull requests are skipped on the persistent runner, including catalog
checks that import project code. A maintainer must review a fork change and
promote it to a trusted repository branch before running these jobs. Deployment
additionally requires a successful same-repository push workflow; pull-request
runs cannot deploy to Soliso.
