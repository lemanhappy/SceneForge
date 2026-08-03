# Contributing to SceneForge

## Development setup

Requirements: Python 3.12, `uv`, Node.js 22 or later, npm, and FFmpeg.

```bash
uv sync --frozen
cd frontend && npm ci && cd ..
cd ui && npm ci && cd ..
```

Copy `configs/agent.example.yaml` to `configs/agent.local.yaml` only when local provider settings are needed. The local file is ignored and must never be committed.

## Before opening a pull request

```bash
uv run python scripts/check_repo_hygiene.py
uv run pytest -q
cd frontend && npm run build
cd ../ui && npm test
```

Keep changes focused, add tests for behavior changes, and update documentation when a public contract changes. Do not commit generated media, logs, local databases, provider credentials, user assets, or build output.

## Assets and integrations

New sample media must include source, author, and license information. New providers must document environment variables, supported capabilities, data sent to the provider, and whether calls incur cost.

## Pull requests

Explain the user-visible outcome, implementation risks, verification performed, and any migration required. By contributing, you agree that your contribution is licensed under the repository MIT License.
