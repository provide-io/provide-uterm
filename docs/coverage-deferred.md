# Deferred coverage waivers (core)

The core package (`provide-uterm`) enforces 100% branch coverage via
`packages/provide-uterm/pyproject.toml`'s `--cov-fail-under=100`. Two modules are
explicitly excluded from that gate (`[tool.coverage.run].omit`) and tracked here
so the waivers stay visible and get paid down rather than silently forgotten.

| Module | Reason deferred |
|---|---|
| `src/provide/uterm/bridge/base.py` | Worker-side `HijackableMixin` checkpoint gating — a substantial async-loop surface (hijack pause/resume/step handshakes) not yet driven by tests. |
| `src/provide/uterm/recording.py` | Asciinema-style recording writer — file-IO and log-rotation branches not yet fully covered. |

Until paid down, these two modules are **not measured** — edits to them can
regress coverage silently, so treat them as higher-risk.

## How the gate runs

The core gate is scoped to `packages/provide-uterm/src/provide/uterm` via a
**repo-root-relative** `--cov` path (an import name can't be used: `provide.uterm`
is a namespace shared by every workspace package). So it must run from the
repository root:

```bash
uv run pytest packages/provide-uterm/tests
```

This is wired into CI (`.github/workflows/ci.yml`, the core job) and the local
gate (`scripts/run_all_tests.py`, the "provide-uterm (core, coverage gate)" suite).

## Paying a waiver down

1. Delete the module's entry from `[tool.coverage.run].omit` in
   `packages/provide-uterm/pyproject.toml`.
2. Run `uv run pytest packages/provide-uterm/tests` and add tests until the gate
   passes at 100%.
3. Remove the module's row from the table above.
