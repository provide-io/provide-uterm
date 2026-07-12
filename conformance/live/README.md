<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Live black-box harness (Layer B)

This directory is the **live** multi-language parity harness described in
`docs/superpowers/specs/2026-07-11-csharp-live-parity-design.md`.

It is **not** the offline codec differential system (`vectors.json` /
`ConformanceVectorsTests`). Those remain Layer A under each language package.

## Layout

| Path | Purpose |
|------|---------|
| `schema/result.schema.json` | Canonical scenario result shape |
| `scenarios/*.json` | Scenario definitions (actions + expectations) |
| language drivers | Spawn `uterm` binaries or in-process hosts |

## Capability tags

Scenarios and results may carry `capabilities` such as:

- `pty.unix`, `pty.conpty`
- `gui_rest` (`go` \| `csharp`)
- `ssh.hostkey`
- `rfb.raw`

Required CI jobs must not silent-skip: either **run**, report **UNSUPPORTED** with
an alternate required scenario, or **fail**.

## First scenario

`scenarios/001_health_sessions.json` — health + session list against a running
server. C# coverage lives in package tests that assert the same contract
in-process; out-of-process drivers land as binaries stabilize.
