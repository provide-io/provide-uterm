# Session Lifecycle Security Conformance Implementation Plan

**Goal:** Add an executable shared public-route parity gate for fragmentation,
browser quotas, governance failure behavior, and resume ownership.

**Architecture:** A JSON contract defines required categories and exact
backend statuses. A central Python runner launches isolated native integration
adapters and validates normalized observations. Native tests boot configured
servers and drive their public HTTP/WebSocket routes.

## Task 1: Contract and runner (TDD)

- Add failing conformance tests for missing categories, missing backend cells,
  silent skips, duplicate observations, unexpected unsupported status, adapter
  timeout/failure, and observation mismatch.
- Add `spec/session_lifecycle_security_scenarios.json` with the exact
  served/unsupported/unserved matrix.
- Implement `scripts/run_session_lifecycle_security_scenarios.py` and make its
  paths independent of the caller's working directory.

## Task 2: Python public-route adapter (TDD)

- Add failing native scenarios for fragmentation, quota accounting/rollback,
  configured signed governance failure, and resume ownership/replay.
- Boot the configured FastAPI server on an ephemeral listener and exercise
  only public HTTP/WebSocket routes.
- Emit normalized observations and make all focused server gates pass.

## Task 3: Go public-route adapter (TDD)

- Add failing native scenarios for fragmentation, quota accounting/rollback,
  configured governance `501`/zero-delivery behavior, and resume ownership.
- Use the real Go server router/listener and public routes.
- Emit normalized observations; run focused, full, race, and vet gates.

## Task 4: C# public-route adapter (TDD)

- Add failing native scenarios for fragmentation, quota accounting/rollback,
  configured governance `501`/zero-delivery behavior, and resume ownership.
- Reuse the real ephemeral `UtermServer` integration fixture, not hub-only
  methods.
- Emit normalized observations; run focused, full serial, and Release build
  gates.

## Task 5: CI, docs, and closure

- Run the shared lifecycle runner and its tests in CI.
- Update protocol/security matrices with the exact status vocabulary.
- Run all-language affected gates and the full conformance suite.
- Obtain independent spec and code-quality reviews.
- Record evidence and close `ARCH-001` only after both reviews approve.
