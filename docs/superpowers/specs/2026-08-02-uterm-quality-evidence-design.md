# uterm Quality and Evidence Convergence Design

## Purpose

Make repository gates, generated capability data, documentation, and release
evidence accurately describe every implementation after the safety and TypeScript
server work lands.

## Capability manifest and conformance

Replace hand-maintained capability claims with a generated manifest derived from
the canonical REST registry, explicit non-registry HTTP/WS inventory, and declared
platform-only operations. Each backend entry records `served`, `unsupported`, or
`platform_specific`, plus the native adapter that proves served behavior.

Validation fails when:

- a required operation lacks a backend classification;
- a served operation lacks executable evidence;
- a handler is bound but omitted from the manifest;
- a portable operation is marked unsupported without an approved reason; or
- generated artifacts differ from committed files.

The existing `spec/uterm-api.yaml` symbol checks remain as a lower-level static
gate and are expanded to TypeScript and semantic modules where useful. They do not
substitute for executable scenarios.

## Tooling corrections

### Python typing

`ci/typecheck.sh` becomes diagnostic-clean and fails on any `ty` diagnostic. All
current diagnostics are fixed in source or precise type declarations: iterator
typing, optional runtime imports, mixin protocols, and dynamic server delegates
receive real typed interfaces. A baseline or ignored diagnostic count is not the
target.

### C# warnings

Release builds are warning-free. Signed bitwise operations, nullable API-key and
metrics flows, dead fields, and platform analyzer findings are corrected or
expressed with narrow, justified platform guards. Global warning suppression is
not used.

### Shell

Shellcheck warnings in language smoke, Colima install, and VNC entrypoint scripts
are fixed while preserving behavior. Script tests or safe dry-run assertions cover
any changed branching or quoting.

### Node version support

The application test environment defines browser storage through jsdom rather than
depending on Node's experimental process-global `localStorage`. Build, typecheck,
lint, and tests run on Node 22, 24, and 26. The package engine ranges and CI matrix
must agree.

### Live Go driver freshness

The live harness never silently executes a stale repository binary. It builds a
fresh driver into a temporary or content-addressed path, or validates source and
module timestamps before reuse. Tests modify a relevant source timestamp or
fingerprint and prove that stale output cannot be selected.

## Documentation truth

`docs/ARCHITECTURE.md` and related diagrams describe all Python packages plus the
Go, C#, TypeScript runtime, TSX application, native modules, shared contract
runners, and generated capabilities. Package READMEs state current coverage floors
and runtime maturity. Server deployment docs explain TypeScript configuration and
platform-specific exclusions after parity is proven.

Numbers that change mechanically, such as test counts or coverage percentages,
are either generated, described as thresholds, or tied to dated evidence. A README
must not present a stale measured percentage as a permanent guarantee.

## Security and dependency evidence

Final verification includes language-appropriate dependency vulnerability, license,
secret, static security, and packaging scans. Findings are triaged by reachability
and recorded; unexplained tool failure or skipped provisioning is not a pass.
Generated frontend assets, frame schemas, capability manifests, lockfiles, package
artifacts, and workflow YAML are checked for drift.

## Final matrix

Verification is ordered from focused to broad so failures remain diagnostic:

1. Finding-specific regression tests and shared semantic runners.
2. Python package suites with prescribed strict coverage and mutation selection.
3. TypeScript runtime build/typecheck/lint/100% coverage and Node smoke.
4. Frontend and TSX build/typecheck/warning-fatal lint/coverage on supported Node
   versions.
5. Go format, test, vet, and race.
6. C# Release build, serial test/coverage gate, analyzers, and packaged binaries.
7. Native C builds, self-tests, exported-symbol checks, and available sanitizers.
8. Shellcheck, static conformance, generated-file checks, audits, workflow parsing,
   and package artifacts.
9. Full client/server live matrix, including the TypeScript Node backend.

Every command, result, relevant version, and justified platform skip is recorded
in the convergence tracker. A red gate reopens its owning finding. Final completion
requires no unexplained unsupported cell, no warning hidden by a permissive wrapper,
and a clean worktree.
