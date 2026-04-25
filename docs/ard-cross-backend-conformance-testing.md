# ARD: Cross-Backend Conformance Testing

## Status

Accepted

## Problem

FastAPI and Cloudflare both expose provide-terminal session APIs, browser flows, and hijack controls, but parity drifts over time.

The drift happens for predictable reasons:

- The implementations are split across different runtimes and storage models.
- Some behaviors are intentionally different, such as FastAPI WebSocket hijack control versus Cloudflare REST-only hijack control.
- Backend-specific tests tend to validate each implementation in isolation instead of asserting the same user-visible contract.
- Shared behavior is described in docs like `docs/protocol-matrix.md`, but the docs are not executable and do not stop regressions.

Without a shared conformance harness, a change can look correct in one backend and still break authz, share-token handling, or hijack/resume semantics in the other.

## Current Behavior

Today the most important cross-backend surfaces are:

- Session visibility and access control.
- Share-token and cookie-based access to session and inspect views.
- Browser hello payloads and capability flags.
- Hijack acquire/send/release behavior.
- Resume token handling.
- Error shape and status codes for unauthorized or unsupported actions.

The `docs/protocol-matrix.md` file already documents the intended contract, but the tests are still largely backend-local. That means the matrix is a reference, not an enforcement mechanism.

## Decision

Create and maintain a shared conformance test suite that runs the same scenario set against both backends, with backend-specific expectations encoded as capability flags rather than ad hoc assertions.

The suite should verify the externally visible behavior of:

- session authz
- share-token access
- browser hello and capability negotiation
- hijack flows
- resume flows

It should not try to force identical internal implementation details.

## Options Considered

### 1. Keep backend-specific tests only

Each backend keeps its own tests and the protocol matrix stays as documentation.

Pros:
- Fast to write.
- Lets each backend optimize for its runtime.

Cons:
- Parity drift continues.
- Bugs can survive because the same scenario is never exercised against both backends.
- The docs become aspirational rather than enforced.

### 2. Add a shared black-box conformance harness

Write one scenario suite that parameterizes over backend fixture, auth mode, and capability matrix.

Pros:
- Directly tests what users see.
- Catches regressions in authz, share, hijack, and resume behavior before release.
- Keeps intentional backend differences explicit.

Cons:
- Requires a little fixture plumbing to stand up each backend in a comparable way.
- Needs a stable contract for backend-specific expected differences.

### 3. Centralize all backend behavior behind one adapter

Push all session/authz/share/hijack logic into a shared core and make both runtimes thin transport layers.

Pros:
- Highest theoretical parity.

Cons:
- Too large a refactor for the current codebase.
- Would not eliminate every runtime-specific difference anyway.
- Slows down practical testing work.

## Recommendation

Use option 2.

The right boundary is a shared conformance suite that treats the two backends as black boxes and asserts the same user-visible contract wherever the protocol matrix says parity exists. Backend-specific tests can stay, but they should no longer be the only guardrail for cross-backend behavior.

## Practical Testing Strategy

### 1. Define the contract in one place

Keep `docs/protocol-matrix.md` as the human-readable source of truth for backend capabilities.

For every behavior in the matrix, define:

- the request or frame used
- the expected response shape
- the backend-specific capability flag or skip condition
- the error code/message for unsupported paths

### 2. Run the same scenario set against both backends

Parameterize the suite over:

- FastAPI app fixture
- Cloudflare app fixture
- auth mode
- presence or absence of a share token
- role combinations (`viewer`, `operator`, `admin`)

Each scenario should exercise the public API only. The same test body should not need to know whether the backend is in-process or running on a Durable Object.

### 3. Focus on the flows that drift

Start with the highest-risk sequences:

- viewer can read or cannot read a session depending on visibility
- share token opens the session page and inspect page with the correct role
- unauthorized users get the same failure shape on both backends
- `hello` advertises the correct capability flags
- hijack acquire/send/release behavior follows the backend contract
- resume token restore either works or is explicitly ignored in the same cases

### 4. Assert backend-specific differences explicitly

The suite should not hide intentional differences. For example:

- FastAPI supports WebSocket hijack control.
- Cloudflare rejects WS hijack frames and requires REST hijack routes.

Those differences should be encoded as expected outcomes, not treated as failures.

### 5. Run conformance in CI

The conformance suite should be part of the normal test gate for both backends.

That means a change to session authz, share-token handling, or hijack logic must pass:

- FastAPI-specific unit/integration tests
- Cloudflare-specific unit/integration tests
- the shared cross-backend conformance scenarios

## Consequences

- Parity regressions become visible immediately instead of being discovered after deployment.
- Docs and tests align: `protocol-matrix.md` describes the contract, and the conformance suite enforces it.
- Backend changes may require updating the matrix and the shared test expectations together, which is the right cost for a contract change.
- Some backend-specific implementation shortcuts will be blocked if they break the public contract.

## Risks

- The shared suite can become brittle if it is written around implementation details instead of externally visible behavior.
- If capability flags are too coarse, the suite may either over-skip or over-constrain intentional differences.
- Without strict fixture discipline, the test harness can duplicate backend setup logic and become hard to maintain.

## Mitigations

- Keep assertions at the HTTP/WS boundary.
- Use a small number of reusable scenario helpers instead of one-off test bodies.
- Treat protocol changes as contract changes and update the matrix first, then the tests.

## Verification

- Add a shared scenario for each authz/share/hijack flow that is known to drift.
- Confirm each scenario passes against both backends in CI.
- Keep the matrix and the suite in sync whenever a backend capability changes.
