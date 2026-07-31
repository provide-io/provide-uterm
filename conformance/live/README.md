<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The live interop matrix (Layer B)

Every client against every server, over a real socket, in every language this
repository ships.

This is **not** the offline differential corpora. Those are Layer A: they live
beside each port and prove that each language *decides* the same things. They
prove nothing about wiring. A port can pass ten thousand corpus tests without
ever having started a server and had a client talk to it.

## Running it

```bash
# what can run, and what cannot
python conformance/live/harness --list-drivers

# the whole matrix
python conformance/live/harness

# one scenario, one server language
python conformance/live/harness --scenario 002_session_authz --servers python
```

It exits non-zero when any cell failed or errored.

## Layout

| Path | Purpose |
|------|---------|
| `PROTOCOL.md` | The driver protocol. Normative — a driver implements this. |
| `schema/scenario.schema.json` | What a scenario may say |
| `schema/result.schema.json` | What a driver reports back |
| `scenarios/*.json` | The contract itself: steps, and what must hold |
| `harness/` | Loading, running, judging, comparing, printing |
| `drivers/python/` | The reference driver |

Other languages' drivers live with their package, because they are built by
that package's toolchain: `packages/provide-uterm-go/cmd/uterm-live-driver`,
`packages/provide-uterm-ts/bin/uterm-conformance.mjs`, and the C# live driver
project.

## The two things a cell is held to

A cell — one client language, one server language, one scenario — passes when
both hold:

1. **Every expectation the scenario wrote down.** These are evaluated by the
   harness, never by a driver, so four languages cannot disagree about what an
   expectation *means* — only about what their server did.
2. **Agreement with the reference cell.** What this cell observed must match
   what the Python-client-on-Python-server cell observed, field for field.

The second is what earns the matrix its keep. A scenario can only assert what
somebody thought to assert; agreement catches the fields nobody thought about,
and that is where parity actually drifts. A field that legitimately differs
between runs — a clock, a generated id — is named by the scenario in a step's
`volatile` list, so what is tolerated is written down rather than guessed at.

## Capabilities, and never skipping silently

A scenario may require capabilities (`"requires": ["hijack.rest"]`). The
harness checks the selected client's registered static capabilities and the
running server's announced capabilities before launching the client, then
validates the capabilities returned by the client as well. A missing capability
produces an explicit `unsupported` cell rather than a silent skip. A driver that
is not built is printed too, with the reason.

Four green cells and sixteen green cells produce the same summary line in
every report ever written. So the count, and every gap, is always printed.
