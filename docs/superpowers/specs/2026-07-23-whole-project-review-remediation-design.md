# Whole-Project Review Remediation Design

## Goal

Resolve all actionable findings from the July 23 whole-project review without
changing public APIs unnecessarily. Every behavior change must be covered by a
regression test that fails before the implementation and passes afterward.

## Approach

Use focused fixes inside the existing subsystem boundaries. Preserve the
current FastAPI, Cloudflare, Go, C#, and frontend architectures rather than
introducing a new cross-language framework. Shared security invariants must
remain equivalent across backends even when their concurrency mechanisms
differ.

## Security and Authentication

### PAM event authorization

The Cloudflare `/api/pam-events` route will resolve the authenticated principal
and require an operator or administrator role before mutating session records.
Viewer JWTs will receive `403`.

FastAPI does not expose a remotely callable PAM event endpoint. Its PAM events
arrive through the local platform listener, so no matching HTTP authorization
change is required. Tests will document this boundary so a future FastAPI PAM
endpoint cannot be added without an explicit authorization decision.

### One-time tunnel invites

Cloudflare invite redemption will move from a KV read-modify-write sequence to
the per-tunnel Durable Object. The object serializes redemption for one tunnel,
clears the invite before returning the bootstrap token, and persists the
updated tunnel state. KV remains the durable registry mirror but is no longer
the concurrency authority for redemption.

FastAPI will retain its synchronous in-process `dict.pop` consumption. The
consume function contains no suspension point, so only one request in a
process can obtain an invite. A concurrency regression test will preserve that
single-use behavior. This design does not introduce Durable Object concepts
into FastAPI.

### Session connector credentials

Quick-connect session definitions will contain only scrubbed connector
configuration. The unsanitized credential fields will be passed separately to
the in-memory session runtime and never included in API responses, session
definitions, audit records, or persistent stores. The runtime will merge the
private values only when constructing the connector and will discard them when
the session is removed.

### Release provenance

The SLSA reusable workflow will be pinned to a verified full commit SHA. The
job's existing release permissions remain unchanged.

## Protocol and Concurrency Correctness

### Cloudflare tunnel framing

Worker socket attachment metadata will distinguish ordinary worker sockets
from binary tunnel sockets, including after hibernation. Browser input and
control messages sent to tunnel sockets will use the existing binary tunnel
encoders; ordinary worker sockets will retain the inline terminal/control
encoding.

### Manager command acknowledgement

Agent state will track a monotonic last-issued command sequence separately
from the optional pending command. Acknowledging a command will clear only the
pending payload, never the sequence counter. Stale reports will be rejected
before their acknowledgement field can mutate pending state.

### Relay and VNC I/O

RFB exact reads will accumulate fragmented reads until the requested length or
true EOF. The share relay will stop when either direction finishes, cancel and
await the sibling task, and close cleanly. Synchronous VNC connection and TLS
setup will run outside the FastAPI event loop.

### Screen-change waits

Calls without an explicit sequence will capture the current change sequence as
their baseline. They will return immediately after the next update and return
false on timeout even when older output exists.

## C# and Go Runtime Corrections

The shipped C# manager entry point will execute the existing asynchronous
manager program so the HTTP listener is started before readiness is reported.
The inspect page will resolve JavaScript and CSS filenames from the Vite
manifest instead of assuming unhashed filenames.

The C# session logger will recursively redact strings in nested dictionaries
and collections. Flushes will retain a batch until storage acknowledges it,
and transient periodic failures will be caught so later intervals retry.

Go and C# JWKS caches will store fetch time, expire entries after a bounded
interval, and perform one synchronized refresh when the requested key ID is
not present. A failed refresh will return an authentication error without
discarding a still-valid cached set for other tokens.

## Packaging

`provide-uterm-annotation` will be added to the release build matrix and the
central published-package metadata. Release artifact verification and
installation checks will therefore cover it alongside the other workspace
distributions.

## Error Handling

- Authorization failures return `403` without revealing whether a target PAM
  session exists.
- Consumed or stale invites return the existing invalid/expired response.
- Tunnel protocol selection fails closed when attachment metadata is invalid.
- Short RFB EOF still raises `EOFError`; ordinary fragmentation does not.
- Recording store errors preserve buffered events and are retried on the next
  interval while explicit flush callers still receive the error.
- JWKS refresh errors remain authentication failures and never accept an
  unverified token.

## Testing

Each fix follows a red-green cycle with a focused regression test. Targeted
package suites will run after each subsystem is changed. Final verification
will include:

- `uv run python scripts/run_all_tests.py`
- Go unit tests and the existing targeted race tests
- C# Release build and test suite
- Frontend typechecks and Vitest suites
- Release/package metadata validation
- A final clean diff and repository status review

## Non-Goals

- No new public authentication mode or credential persistence service.
- No broad rewrite of the Cloudflare runtime or FastAPI registry.
- No visual redesign of the inspect UI.
- No unrelated cleanup or dependency upgrades.
