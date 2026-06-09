# Deferred coverage waivers (core)

The core package (`provide-uterm`) enforces 100% branch coverage via
`packages/provide-uterm/pyproject.toml`'s `fail_under = 100`. Modules that are
not yet under that gate are listed in `[tool.coverage.run].omit` and tracked
here so the waivers stay visible and get paid down rather than silently
forgotten.

## Currently deferred

**None.** Every module under `src/provide/uterm` is measured by the 100% gate.

The two historical waivers were paid down on 2026-06-08:

| Module | How it was closed |
|---|---|
| `src/provide/uterm/bridge/base.py` | `HijackableMixin` pause/resume/step + watchdog branches driven by `TestHijackableMixinBranches` (the watchdog tests wait past the loop's `max(0.5, …)` sleep floor so the body actually runs). |
| `src/provide/uterm/recording.py` | Already fully exercised by the existing `test_recording_*` suites — the omit was purely conservative; removing it measured the module at 100% with no new tests. |

## How the gate runs

The gate is scoped to `src/provide/uterm` via the **package-relative** `source`
path in `[tool.coverage.run]`. coverage.py resolves that path against the
current working directory, so the gate **must run with the package as CWD**
(not from the repo root, where `src/provide/uterm` would resolve to nothing and
report 0%):

```bash
uv run --directory packages/provide-uterm pytest tests
```

This is how CI runs it (`.github/workflows/ci.yml`, the core coverage step) and
the local gate (`scripts/run_all_tests.py`, the "provide-uterm (core, coverage
gate)" suite).

## Deferring a module (only if genuinely necessary)

1. Add the module path to `[tool.coverage.run].omit` in
   `packages/provide-uterm/pyproject.toml` with an inline comment pointing here.
2. Add a row to a "Currently deferred" table above with the reason.

## Paying a waiver down

1. Delete the module's entry from `[tool.coverage.run].omit`.
2. Run `uv run --directory packages/provide-uterm pytest tests` and add tests
   until the gate passes at 100%.
3. Move the module's row out of "Currently deferred" (or note it as closed).
