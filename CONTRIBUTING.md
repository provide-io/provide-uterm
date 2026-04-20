# Contributing to provide-terminal

Thanks for contributing to provide-terminal — the web-based terminal emulator + session/hijack control plane. This guide covers setup, quality gates, and submission expectations for the workspace and its 5 packages.

See `CLAUDE.md` for the detailed architectural rules that govern code review.

## Prerequisites

- Python 3.11+
- `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Node.js 20+ and pnpm (for the frontend under `packages/provide-terminal-app/`)
- Playwright browsers (`uv run playwright install chromium`) for e2e tests

## Development Setup

```bash
git clone https://github.com/provide-io/provide-terminal
cd provide-terminal
uv sync --all-extras --all-packages
```

The repo is a `uv` workspace; one `uv sync` wires up all 5 packages as editable siblings.

## Packages

- `provide-terminal` — core emulator + shared types
- `provide-terminal-client` — HTTP/WebSocket/SSH client transports
- `provide-terminal-platform` — PTY, capture, manager
- `provide-terminal-server` — FastAPI server + hijack control plane + frontend assets
- `provide-terminal-cloudflare` — Cloudflare Workers runtime

## Quality Gates

Before opening a PR:

```bash
make quality         # ruff lint + format, mypy strict, pytest with coverage gate
make test            # unit + integration (excludes playwright, memray, mutant)
```

Requirements:

- **100% branch coverage** across all 5 packages (enforced).
- **mypy strict**. No `type: ignore` without an inline justification.
- Files ≤ 500 lines.
- SPDX headers on every source/config file (`AGPL-3.0-or-later`).

## Commits

- Conventional prefixes: `feat(hijack): …`, `fix(gateway): …`, `refactor(pty): …`, `test(e2e): …`, `docs: …`, `chore: …`.
- Subject ≤ 72 chars.
- Do not mention AI assistance. No `Co-Authored-By:` trailers.
- Canonical email: `code@tim.life` or `code@provide.io`.

## Pull Requests

1. Run `make quality` (must pass).
1. For hijack/control-channel changes, run `packages/provide-terminal/tests/hijack/`.
1. For UI changes, include a Playwright snapshot: `make test-playwright`.
1. PR description notes any protocol or security impact.
