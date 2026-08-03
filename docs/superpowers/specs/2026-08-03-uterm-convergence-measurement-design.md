# uterm Convergence Measurement and Defect Inventory

## Status

**Measured 2026-08-03 against `main` at `17ebba29`.**

Every row below is an observation valid on that date only. Rows are not
guarantees and must not be cited as current state without re-running the
re-measure command recorded beside them.

This document records *where the implementation sits* against the four
2026-08-02 convergence designs. It does not restate, supersede, or amend those
designs — they remain the contracts. It adds only what they lack: a dated
measurement, a defect inventory with stable IDs, and a decomposition into
implementation plans.

Explicitly out of scope: how to fix anything. Fixes belong in the plans listed
in "Decomposition".

## Why this document exists

The four 2026-08-02 designs describe a target state across Python, TypeScript,
Go, C#, native C, and shell tooling. Each earlier remediation wave
(2026-07-31) produced a design *and* a matching implementation plan under
`docs/superpowers/plans/`. The 2026-08-02 wave produced four designs and zero
plans. Nothing tracked which of their roughly twenty targets had been built.

Measurement finding: **two of them had.** The designs read as a description of
shipped state and were not one.

The designs themselves are sound. Every premise checked below is real and still
present in the tree. They are an accurate defect list and an inaccurate status
report. This document supplies the missing status.

## Measurement method and its limits

Each cell was established by reading the named file at the named line, or by a
repository-wide search whose command is recorded. Cells assert only what was
checked.

Limits, recorded so no reader over-trusts the table:

- Severity and reachability in the defect inventory are an engineering read,
  not a triaged security assessment. No exploit was written for any finding.
- Rows marked ⚠️ were sampled, not exhaustively enumerated. This applies to
  TypeScript and Go reaping scope, and to per-port route coverage.
- Live-matrix `unsupported` cells were not enumerated at all. CM-07 surfaces
  them.
- Two claims made earlier in the measurement session were wrong and are
  corrected here rather than quietly dropped:
  - TypeScript **does** validate its handler table
    (`packages/provide-uterm-ts/src/server/route-binding.ts:112`). An earlier
    claim that it did not was based on searching `app.ts` alone.
  - Go has **five** unbounded WebSocket read paths, not three. The first count
    missed `client/controlws.go` and `gateway/pump.go`.

Legend: ✅ meets design · ⚠️ partial · ❌ absent or defective · — not applicable

## Measurement: semantic and safety convergence

Contract: `2026-08-02-uterm-semantic-safety-convergence-design.md`

| Target | Python | TypeScript | Go | C# | Native C |
|---|---|---|---|---|---|
| Bounded WS reads, 1 MiB | ✅ | ✅ | ❌ | — | — |
| Descriptor-safe append | ✅ | ✅ | ✅ | ❌ | — |
| Explicit `ITx` on every store op | — | — | — | ❌ | — |
| Ambient transaction slot removed | — | — | — | ❌ | — |
| Reaping in both engines | ✅ | ⚠️ | ⚠️ | ✅ | — |
| Shared language-neutral rule artifact | ❌ | ❌ | ❌ | ❌ | — |
| Length-aware sockaddr formatting | — | — | — | — | ❌ |
| Six new scenario families | ❌ | ❌ | ❌ | ❌ | ❌ |

Evidence and re-measure commands:

**Bounded WS reads.** Python bounds both the worker link and the server routes
(`packages/provide-uterm-server/src/provide/uterm/server/bridge/worker_link.py:129`
default `1_048_576`, applied at `:273` and `:366`;
`.../bridge/routes/websockets_impl.py:124`). TypeScript bounds via
`packages/provide-uterm-ts/src/gateway/ws-server.ts:187` (`maxPayload`) and
`packages/provide-uterm-ts/src/cloudflare/config.ts:374`. Go bounds its *server*
paths (`server/ws_worker.go:59`, `server/ws_browser.go:196`,
`server/ws_tunnel.go:48`, `bridge/worker_link.go:371`) but leaves five client
paths unbounded. See CM-01.

```
grep -rn "SetReadLimit" packages/provide-uterm-go --include="*.go" | grep -v _test
```

**Descriptor-safe append.** Python
(`packages/provide-uterm/src/provide/uterm/file_io.py:30`), Go
(`packages/provide-uterm-go/fileio/fileio.go:52`) and TypeScript
(`packages/provide-uterm-ts/src/file-io/file-io.ts:65`) all open with
`O_NOFOLLOW` and operate on the resulting descriptor. C# does not. See CM-02.

```
grep -rn "O_NOFOLLOW\|NoFollow" packages/*/src packages/provide-uterm-go --include="*.py" --include="*.go" --include="*.ts" --include="*.cs"
```

**C# transactions.** `ITx` is declared at
`packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs:15` and
returned by `BeginAsync` (`:186`, `:245`, `SqliteEngine.cs:204`). No store
method accepts one. The ambient slot survives at `SqliteEngine.cs:87`, `:213`,
`:225`, where `Command()` binds `cmd.Transaction = _current.Inner`. See CM-03.

```
grep -rn "ITx" packages/provide-uterm-csharp/src --include="*.cs"
grep -rn "ITx tx" packages/provide-uterm-csharp/src --include="*.cs" | wc -l   # expect 0
grep -rn "_current" packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/SqliteEngine.cs
```

**Reaping.** Both C# engines implement it
(`ControlPlane/Engine.cs:247`, `ControlPlane/SqliteEngine.cs:250`). Entity scope
against the design's list — expired sessions, expired or revoked session and
resume tokens, expired or deleted leases, resolved approvals past retention —
was not verified per-entity and is the ⚠️ in the TypeScript and Go columns.

```
grep -rn "ReapAsync" packages/provide-uterm-csharp/src --include="*.cs"
```

**Detector rules.** No language-neutral rules artifact exists anywhere in the
repository, so no port can consume one; each maintains its own inventory. The
Python annotation package holds the de-facto canonical inventory in Python
source, which is why the design names it as the oracle, but it is not published
in a form another language can read. C# carries
`src/Provide.Uterm/Annotation/Detector.cs`. Whether the per-port inventories
currently agree was not measured — CM-06 establishes that by fixture rather than
by inspection. See CM-06.

```
find . -name "rules.json" -not -path "*/node_modules/*"
```

**Native sockaddr.** `capture.c:217` formats with
`snprintf(addrstr, sizeof(addrstr), "unix:%s", un->sun_path)`, which scans to a
NUL terminator and ignores the `addrlen` the hook supplies. See CM-04.

**Scenario families.** `spec/` contains `behavior.json`,
`behavior_vectors.json`, `fanout_security_scenarios.json`,
`session_lifecycle_security_scenarios.json`, `uterm-api.yaml`,
`validate_conformance.py`, `_conformance_extractors.py`. None of the six
families named by the design is present.

```
ls spec/
```

## Measurement: TypeScript server parity

Contract: `2026-08-02-uterm-typescript-server-parity-design.md`

| Target | Python | TypeScript | Go | C# |
|---|---|---|---|---|
| Canonical route registry | ✅ | ✅ | ✅ | ✅ |
| Handler-table validation at bind | — | ✅ | — | — |
| Every portable route served | ✅ | ⚠️ | ⚠️ | ⚠️ |
| 100% line/branch/statement/function coverage | ✅ | ✅ | ❌ | ⚠️ |
| Live matrix, no unexplained `unsupported` | ✅ | ❌ | ✅ | ✅ |

`API_ROUTE_REGISTRY` is defined at
`packages/provide-uterm-ts/src/api-routes/routes.ts:297`. Binding validates that
every capability has a handler
(`packages/provide-uterm-ts/src/server/route-binding.ts:112`) and refuses to
publish a role-guarded route with no authorizer (`:118`).

TypeScript coverage was confirmed this session at 10,610 tests with 100% on all
four metrics. Python enforces 100% branch and line via `--cov-fail-under=100`.

Python's ✅ for route coverage is definitional rather than measured: the design
names the Python server as the contract's source, so it serves the inventory by
construction. The three ⚠️ marks are unmeasured, not known-incomplete.

The TypeScript package README still describes a partial runtime port. Whether
that is accurate depends on route enumeration this measurement did not perform;
CM-07 resolves it.

## Measurement: quality and evidence

Contract: `2026-08-02-uterm-quality-evidence-design.md`

| Target | Python | TypeScript | Go | C# | Native C |
|---|---|---|---|---|---|
| Generated capability manifest | ❌ | ❌ | ❌ | ❌ | ❌ |
| Type gate fails on any diagnostic | ❌ | ✅ | ✅ | ⚠️ | — |
| Release build warning-free | — | ✅ | ✅ | ❌ | — |
| Node 22/24/26 matrix | — | ❌ | — | — | — |
| Shellcheck gate | ❌ | ❌ | ❌ | ❌ | ❌ |
| Live Go driver freshness | — | — | ❌ | — | — |

The C# ⚠️ for the type gate reflects that its analyzer configuration was not
inspected; only `TreatWarningsAsErrors` was. Mutation gates are not a target of
this contract and are therefore not tabulated; for the record, Python and C#
run one and TypeScript and Go do not.

**`ty` is informational only.** `ci/typecheck.sh:51` says so in a comment
("Informational only — never fail the gate"); `:60` runs
`uv run ty check "${pkg}" || rc=$?`; `:64` emits
`::warning::ty reported issues (informational only)`; `:66` is an unconditional
`exit 0`. The design requires the opposite. See CM-08.

**C# warnings are not fatal.**
`packages/provide-uterm-csharp/Directory.Build.props:10` sets
`<TreatWarningsAsErrors>false</TreatWarningsAsErrors>`. CS0675, CS8600 and
CS8603 were observed during a Release build this session. See CM-10.

**Node version is singular.** All eight CI setup steps use
`node-version-file: ".nvmrc"` (Node 22). The engine ranges already disagree:
root `package.json:5` declares `>=20`, `packages/provide-uterm-ts/package.json:9`
declares `>=22`. See CM-11.

```
grep -n "node-version" .github/workflows/ci.yml
```

**Shellcheck does not exist.** Zero occurrences in `.github/workflows/` or
`ci/`. The design speaks of fixing shellcheck warnings in named scripts; there is
no gate to produce them. See CM-09.

```
grep -rn "shellcheck" .github/workflows/ ci/
```

**Go live-driver freshness.** No `go build` invocation in
`tests/conformance/live/`. See CM-12.

## Measurement: operator input-mode lock

Not named by any of the four designs. Found while measuring; recorded because it
is a live cross-language divergence.

| | Python | TypeScript | Go | C# |
|---|---|---|---|---|
| Operator-lock guard | ✅ | ✅ | ✅ | ✅ |
| Operator-lock release | ✅ | ❌ | ❌ | ❌ |

Python guards at
`packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/connection.py:241`
and clears the flag at `:257`
(`st.input_mode_set_by_operator = False`). The three ports carry the guard
(`packages/provide-uterm-ts/src/hub/connection.ts:254`,
`packages/provide-uterm-go/hub/connection.go:172`,
`packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs:354`) and
none carries the release. Once set, the lock never clears. See CM-05.

## Defect inventory

Severity is an engineering read, not a triaged assessment.

| ID | Finding | Languages | Contract | Severity |
|---|---|---|---|---|
| CM-01 | Five WebSocket client paths read without a size limit | Go | semantic-safety | high |
| CM-02 | Secure append authorizes a path, then opens and chmods that path again | C# | semantic-safety | high |
| CM-03 | Control-plane stores share an ambient transaction slot | C# | semantic-safety | high |
| CM-04 | Unix sockaddr formatting scans past the supplied length | native C | semantic-safety | high |
| CM-05 | Operator input-mode lock never releases | Go, C#, TS | none (new) | medium |
| CM-06 | No shared annotation rule artifact; each port maintains its own | all (C# leads) | semantic-safety | medium |
| CM-07 | No generated capability manifest | all | quality-evidence | medium |
| CM-08 | `ty` diagnostics are reported and then discarded | Python | quality-evidence | medium |
| CM-09 | No shellcheck gate exists | shell | quality-evidence | low |
| CM-10 | Release builds tolerate warnings | C# | quality-evidence | low |
| CM-11 | Single Node version; engine ranges disagree | TypeScript | quality-evidence | low |
| CM-12 | Live harness may execute a stale Go driver | Go | quality-evidence | low |

Reachability, stated plainly:

- **CM-04** reads adjacent process memory into log output. Triggered by any peer
  whose socket address the hook formats.
- **CM-02** permits a symlink swapped in between open and `chmod` to receive the
  mode intended for the target. Requires a local user on a shared host.
- **CM-01** permits one oversized frame to drive unbounded allocation. These are
  client paths, so the hostile party is a compromised or malicious server,
  tunnel peer, or gateway upstream — not an arbitrary internet caller.
- **CM-03** needs no attacker. Concurrent unrelated requests can join one
  another's transaction, producing lost updates and isolation that does not hold.
- **CM-05** latches on any operator mode change and degrades availability of
  mode switching thereafter.
- **CM-06** produces annotation divergence between ports with no security
  consequence identified.

## Decomposition

Twelve plans, one per finding. Each defect plan carries its own conformance
scenario family so that it is independently shippable: the family goes red
before the fix and green after, in every language the finding names. Bundling
all families into one prerequisite plan was considered and rejected — it would
block five defect plans behind one.

The families extend the existing conformance architecture
(`tests/conformance/backends.py`, `spec/*.json`); they do not replace the
working fan-out and lifecycle runners.

Defect plans:

| ID | Plan | Scenario family |
|---|---|---|
| CM-01 | Bounded Go WebSocket reader | WS size boundaries, fragmented over-limit |
| CM-02 | Descriptor-safe C# append | append via ordinary files, symlinks, swaps, permission changes |
| CM-03 | Explicit C# `ITx` (breaking change) | transaction isolation, read-your-writes, rollback, conflict, reaping |
| CM-04 | Length-aware native sockaddr | unix, abstract-unix, IPv4, IPv6, truncated, unnamed |
| CM-05 | Operator input-mode lock release | input-mode lifecycle (**new family**, not anticipated by any design) |
| CM-06 | Canonical annotation rule artifact | one-shot and arbitrarily-chunked streaming |

Gate plans:

| ID | Plan | Note |
|---|---|---|
| CM-07 | Generated capability manifest | carries the route/capability inventory family |
| CM-08 | Make `ty` diagnostic-clean and enforce it | removes the `exit 0` wrapper |
| CM-09 | Introduce a shellcheck gate | built from zero |
| CM-10 | Warning-free C# Release build | |
| CM-11 | Node 22/24/26 matrix and engine agreement | |
| CM-12 | Freshness-safe Go live driver | |

Ordering: the plans are independent except **CM-10 before CM-03**, so the C#
API break lands into a build that already treats warnings as errors rather than
adding warnings that nothing catches.

Each plan is written under `docs/superpowers/plans/` and cites its CM ID, this
measurement, and the 2026-08-02 contract it serves.

## Definition of done

A defect row is done when its scenario family fails before the change and passes
after, in every language the row names, and the re-measure command in this
document returns the opposite of what it returned on 2026-08-03.

Code changed without a family that went red first does not close a row. That bar
is the four designs' own; nothing currently enforces it, which is how twenty
targets went untracked.

## Re-measuring

This document is re-runnable. Execute the commands recorded beside each section
and compare against the stated verdicts. No bespoke measurement tooling is
introduced — CM-07's manifest generator is the durable replacement for this
process, and building a second one first would only create something to
maintain.

When re-measured, update the Status date and the affected rows in place. Do not
present a row from an earlier date as current state.
