# Design Review: Standalone C# Live Transport and GUI Parity

**Document:** `docs/superpowers/specs/2026-07-11-csharp-live-parity-design.md`
**Branch / worktree:** `review/csharp-live-parity-design`
**Method:** three parallel read-only subagents (architecture, parity-vs-code, security/CI) + synthesis
**Date:** 2026-07-11
**Verdict:** **Needs revision** before implementation plans

---

## Executive summary

Direction is right: black-box multi-language parity, no C# MCP binary, no VM hosting, harness-first sequencing, and the live-path residual debt in C# is real.

The document is **not implementable as written**. It asserts unnamed “common contracts,” understates current stubs (especially PTY, SSH host-key, GUI/RFB, SSH gateway), conflates offline `vectors.json` conformance with a live harness, and sets completion bars (three-OS matrix, 97% + live paths, C# mutation, no skips, full MCP GUI) that conflict with repo reality (no C# CI job, coverage exclusions, no Stryker, GUI REST only on Go today).

---

## Consolidated severity table

| Sev | Topic | Sources |
|-----|--------|---------|
| **critical** | GUI/RFB architecture missing (no attach path, hub `GraphicalSession`, `/gui/*` routes, human-relay) | arch, parity |
| **critical** | MCP phase assumes REST/GUI contracts never sequenced; Python server also lacks GUI REST (only Go has it) | arch, parity |
| **critical** | “Common contracts” never named (routes, JSON shapes, auth, error envelopes, oracle language) | parity, arch |
| **critical** | Live harness ≠ existing `ConformanceVectorsTests` / `vectors.json` (offline codecs only) | parity, arch |
| **critical** | No C# monorepo CI today; three-OS × 5 partitions is a greenfield cost cliff | security-ci |
| **critical** | Mutation as completion requirement invents tooling (no Stryker) | security-ci, parity |
| **critical** | 97% floor + keep exclusions removed + live paths = gate contradiction | security-ci, parity |
| **critical** | SSH “secure by default” is a false positive (path presence only; proxy hardcodes insecure) | security-ci, parity |
| **major** | No Current State, Architecture, Key Decisions, Alternatives, Risks, Open Questions, real PR plan | arch |
| **major** | Harness underspecified (location, schema, drivers, capability tags, oracle ownership) | arch, parity |
| **major** | Delivery sequence delays three-OS, under-slices PTY/ConPTY, couples MCP to widest matrix | arch, security-ci |
| **major** | Coverage residual / “no silent skips” tension; need capability-tagged outcomes | all |
| **major** | PTY is process pipes today; Go (not Python) is the PTY oracle | parity, security-ci |
| **major** | RFB over-promises vs Go litevirt/Raw-only; CopyRect not a shared baseline | parity |
| **major** | Redaction is opt-in helper, not pipeline guarantee for logs/artifacts | security-ci |
| **major** | Buffer/origin/RFB alloc limits incomplete vs security bullets | security-ci |
| **major** | Threat model missing (trust boundaries, DoS, lease confused deputy, RFB security types) | security-ci |
| **minor** | Gateway IAC / pump lifecycle under-specified vs existing C# gateway pieces | arch, parity |
| **minor** | PR plan is phase titles only | arch |
| **minor** | QEMU optional policy needs runner allowlist | security-ci |
| **nit** | “Match or improve upon” unbounded; glossary/in-scope list thin | arch, parity |

---

## Current-state inventory (C# today)

| Design workstream | C# status | Evidence |
|---|---|---|
| PTY / process | **stub / non-PTY** | process pipes; `OpenHostPty` throws |
| Socket / WebSocket | **partial** | loops exist; no fragment reassembly / hard limits |
| Telnet | **partial** | client minimal IAC; gateway drive, no full negotiator |
| SSH client | **partial (unsafe host-key)** | SSH.NET shell; known_hosts not applied |
| SSH gateway | **missing / CLI rejected** | accept-only; “not yet wired” |
| RFB client | **missing** | tracker raw blit only |
| GUI + lease | **stub** | memory session; no server routes |
| External MCP (terminal) | **plausible** after goldens | hijack REST exists |
| External MCP (GUI) | **blocked** | needs GUI REST + RFB + hub state |
| Offline codec conformance | **production** | `vectors.json` — not live harness |
| Live harness | **missing** | greenfield |
| Coverage residuals | **active** | PTY + live transports excluded |
| Mutation (C#) | **missing** | no tooling |
| Monorepo CI (C#) | **missing** | local `make quality-gate` only |

**Oracle notes:** Python owns codec vectors; Go owns real PTY, SSH known_hosts, SSH/telnet gateways, GUI REST + litevirt RFB path; Python server has no `/gui/*`.

---

## What the design does well

- Clear product boundary: standalone .NET, no Python/Go sidecar, no VM hosting; QEMU test-only.
- Parity as observables (bytes/frames/timeouts/cleanup/leases), not class-layout cloning.
- Honest non-goal: no C# MCP binary; adapters as consumers.
- Harness-first sequencing instinct; protocol-partitioned CI for attribution.
- Security themes are the right themes (host-key, secrets, alloc bounds, lease gate, bounded cancel).
- Workstreams map to real residual debt in `packages/provide-uterm-csharp`.

---

## Minimum revision package (re-review bar)

1. **Current State** table (use inventory above) + residual coverage policy per phase.
2. **Architecture** for GUI/RFB: attach path, hub ownership, REST inventory for MCP, human-relay in/out for v1.
3. **Contract appendix**: routes, JSON goldens, auth modes, error envelopes, oracle language per surface; capability tags for `gui_rest: go|csharp` (not python).
4. **Harness ADR**: Layer A = existing vectors (codecs); Layer B = new live scenarios (schema sketch, first 3 scenarios, driver binary discovery). Do not extend `vectors.json` for live I/O.
5. **Key Decisions + Alternatives + Risks + Open Questions**.
6. **Delivery sequence** with per-phase exit criteria:
   - CI: ubuntu csharp-quality first; OS-specialized PTY/ConPTY; path filters; MCP Linux-first.
   - Coverage: separate unit floor vs live harness gate; exclusion ratchet explicit.
   - Mutation: name Stryker + Go-like pure-function perimeter; not all live paths.
   - Replace absolute “no skips” with capability-tagged required outcomes.
7. **Security**: short threat model; real SSH host-key scenarios; redaction enforcement points; limit table (WS/control/RFB/queues).
8. **Split Workstream 6**: 6a terminal MCP; 6b GUI MCP (blocked on server GUI + RFB).
9. **Tighten Goal**: “observably match” (+ capability-tagged supersets only); freeze RFB endpoint types (raw TCP fixture vs optional litevirt).
10. **Real PR plan** per phase (packages, deps, CI cells, exit tests) — or require `docs/superpowers/plans/` per phase before coding.

---

## Agent artifacts

| Agent | File |
|-------|------|
| Architecture / completeness | `$TMPDIR/grok-501/grok-design-review-c9f1536c-arch.md` |
| Parity gap vs code | `$TMPDIR/grok-501/grok-design-review-c9f1536c-parity.md` |
| Security / CI / testing | `$TMPDIR/grok-501/grok-design-review-c9f1536c-security-ci.md` |

---

## Status of issues

All consolidated issues above: **Status: open** (review-only; design not rewritten in this pass).
