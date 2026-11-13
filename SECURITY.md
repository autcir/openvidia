# Security policy

## Reporting

Report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/autcir/openvidia/security/advisories/new)
when available. If that feature is unavailable, use the maintainer contact
already listed in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Do not include
credentials or exploit details in public issues.

Include the affected version/commit, operating system, reproduction steps,
impact and a redacted example where possible. Maintainer responses and fixes
are best effort; this project does not promise a fixed response deadline.
Fixes are developed on `main` for the current release line. Older versions do
not have a guaranteed backport policy; use the latest published release and
check its release notes.

## Access boundaries

- The proxy binds IPv4 and, where available, IPv6 loopback. It is intended for
  a trusted local user environment.
- Administrative `/api/*` and `/ops/*` requests require the control token and
  are checked for permitted browser origins and host headers. The dashboard
  receives its token at launch and stores it in session storage.
- Inference `/v1/*` is deliberately unauthenticated. Any process able to reach
  it can consume the configured provider quota. The control token does not
  protect this inference endpoint.
- A reverse proxy, SSH forward or tailnet forwarding rule can make a loopback
  service reachable by other users. TLS alone does not restrict access. Add
  independent authentication and authorization before sharing access, or
  keep the service local.
- API keys and control tokens are cleartext local secrets. File permissions
  reduce accidental exposure; they do not protect against malicious code
  running as the same user.

## Data and credentials

Model requests leave the machine for NVIDIA or an explicitly configured
fallback provider. Installing a local proxy does not make inference offline.
Review the destination before submitting sensitive prompts or tool results.
NVIDIA Build metadata is discovery information, not proof of API access or
model capability; selectable IDs must still come from the API catalog.

Never commit key files, tokens, provider credentials or unredacted logs.
Revoke exposed credentials with their provider. Do not publish dashboard URLs
containing `?token=`, screenshots of revealed keys, or configuration backups.
Fallback provider keys are resolved from the explicitly configured environment
variable names; protect the service environment as well as configuration files.

## Updates and automation

Managed updates verify downloaded wheel hashes against PyPI metadata and
stage candidates before activation. Hash verification is not an independent
signature or a defense against a compromised package registry/publisher.
Read [updates](docs/updates.md) for installation scope, checks and rollback.

External-fork code must not run on the persistent self-hosted runner. Current
CI and catalog jobs skip external-fork PRs; maintainers should review changes
and test them in an isolated environment. Authentication failures, unauthorized
administrative access, leaked keys, unsafe update activation and remote-data
injection in the dashboard are appropriate private security reports.
