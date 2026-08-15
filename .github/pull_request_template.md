<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

## What this changes

<!-- One or two sentences. What behaviour is different afterwards? -->

## Evidence

<!--
Commands you ran and their result. "Tests pass" is not evidence; the command
and its tail are. See docs/roadmap/uterm-risk-ranked-action-plan.md.
-->

- [ ] `make quality-gate` (or the package's own gate) passes locally

---

## Protocol change checklist

Skip this section only if the diff touches none of:
`spec/`, `packages/provide-uterm/src/provide/uterm/bridge/schemas.py`, or any
port's `MIN`/`MAX`/`PREFERRED_PROTOCOL_VERSION` declaration.

- [ ] **Every port moved together.** The protocol-version triple is declared in
      six places — Python and TypeScript once each, Go and C# twice each (bridge
      *and* shell). `scripts/check_protocol_drift.py` enforces agreement.
- [ ] **`docs/protocol-matrix.md` reflects the change.** A protocol source that
      moves without the matrix fails CI on pull requests.
- [ ] **Frame schemas regenerated.** `uv run python scripts/codegen_frames.py`,
      then commit `schemas.py`, `frames.schema.json`, and `frames.ts` together.
- [ ] **Corpora regenerated where the change is observable**, and the twinned
      copies of `signature_corpus.json` (Go and C# test trees) stay identical.
- [ ] **Behaviour contracts updated.** New capability cells in
      `spec/session_lifecycle_security_scenarios.json` or
      `spec/fanout_security_scenarios.json` carry an executable adapter in every
      backend that claims them — a `served` cell with no adapter is a false
      claim, and `--validate-only` will say so.
- [ ] **Cloudflare divergence recorded.** If the change lands differently on the
      edge runtime, `docs/cloudflare-divergence-matrix.md` gains or updates a
      row, with a test pinning the edge behaviour.
- [ ] **No new parity claim without evidence.** TypeScript remains a partial
      backend; do not mark a TS cell `served` without a mounted server surface.
      See `docs/parity-labels.md` for what each label means.
