# Security Policy

## Supported versions

Security fixes are applied to the latest released SceneForge version. Development snapshots may change without compatibility guarantees.

## Reporting a vulnerability

Use the repository's private vulnerability reporting or Security Advisory feature. Do not include API keys, access tokens, private prompts, user media, or exploit details in a public issue. If private reporting is unavailable, open a short issue asking the maintainers to establish a private contact channel without disclosing the vulnerability.

Include the affected version, impact, reproduction conditions, and a minimal proof of concept with secrets removed. Maintainers should acknowledge a report within seven days and coordinate disclosure after a fix is available.

## Deployment boundary

SceneForge is local-first and binds to `127.0.0.1` by default. Treat any non-loopback deployment as a network service:

- Require `SCENEFORGE_WEB_TOKEN` or `--token`.
- Place TLS and access control in front of the service.
- Do not expose local media or configuration directories directly.
- Avoid long-lived access tokens in URLs, screenshots, logs, or browser history.

## Credentials

Store provider keys in the ignored `configs/agent.local.yaml` file or environment variables. Never add credentials to pipeline templates, tests, bug reports, logs, or generated artifacts. If a credential enters Git history, revoke it before rewriting the history.
