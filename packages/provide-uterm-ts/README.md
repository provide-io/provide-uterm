<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# provide-uterm-ts

TypeScript port of the provide-uterm terminal session platform, wire-compatible
with the Python implementation. Runs on Node.js >= 22 and is built with
TypeScript 7.

This is the fourth full port, alongside `provide-uterm-go` and
`provide-uterm-csharp`. It is distinct from the two browser packages:

| Package | Scope |
|---|---|
| `provide-uterm-frontend` | Browser widgets (lit + xterm.js), shipped to the SPA |
| `provide-uterm-app` | Browser SPA shell (React) |
| **`provide-uterm-ts`** | **Runtime port: core library, hub, server, client, CLI** |

## Compatibility contract

The Python packages remain the reference implementation. Every module that
touches a wire format or an observable text transformation is proven against
CPython with a **differential corpus**: a `testdata/gen_*_golden.py` script
runs the Python code (via `uv run` at the repository root) over a
deterministic corpus of inputs and records the outputs. The corpus is
committed so CI re-verifies parity without a Python toolchain, and the
TypeScript tests must match it byte-for-byte.

Regenerate a corpus after a reference-implementation change:

```bash
uv run python packages/provide-uterm-ts/testdata/gen_colors_golden.py
```

| Module | Parity evidence |
|---|---|
| `colors` | 894-record CPython corpus (RGB quantisation, SGR rewriting, text + latin-1 byte paths) |
| `defaults` | Constant-for-constant assertion against `TerminalDefaults` |
| `pycompat` | CPython `round()` tie-breaking table |

## Development

```bash
npm run test:ts            # vitest
npm run test:ts:coverage   # vitest + 100% line/branch/function gate
npm run typecheck:ts       # tsc 7, strict
npm run lint:ts            # biome
```

### Test-driven development

Every module in this port is written test-first:

1. **RED** — generate the golden corpus from CPython, write the test file
   against the module's intended public API, and add a stub module whose
   functions throw. Run the suite and confirm *every* new test fails on
   behaviour rather than on a missing import.
2. **GREEN** — implement until the suite passes.
3. **REFACTOR** — clean up with the suite staying green.

Expected values are never hand-guessed. Anything not covered by a generated
corpus is taken from a recorded `uv run python -c ...` invocation and the
source is noted in a comment beside the assertion.

### Conventions

- 100% line, branch and function coverage is enforced by
  `vitest.config.ts`. Barrels (`index.ts`) and `src/testing/` are excluded as
  pure re-exports and test scaffolding.
- ESM with explicit `.ts` import specifiers, rewritten to `.js` on build.
- `src/pycompat/` holds CPython semantic shims (rounding, and later string
  and encoding behaviour) so the same reference semantics are not
  re-derived per module.
- Biome disables `noControlCharactersInRegex` for this package: ANSI escape
  parsing needs literal `\x1b` in patterns.
- New files carry the SPDX header block.
