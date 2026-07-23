# Whole-Project Review Remediation Design

## Goal

Resolve all actionable findings from the July 23 whole-project review without
changing public APIs unnecessarily. Every behavior change must be covered by a
regression test that fails before the implementation and passes afterward.

## Approach

Use focused fixes inside the existing subsystem boundaries. Preserve the Go,
C#, and frontend architectures. Replace the duplicated FastAPI and Cloudflare
`/api/**` routing implementations with one Python `RouteDef` contract layer;
this is deliberately a local backend refactor, not a new cross-language
framework. Shared security invariants must remain equivalent across backends
even when their concurrency mechanisms differ.

## Shared API Route Contract

All shared HTTP API routes are declared exactly once as immutable `RouteDef`
values. A definition records its HTTP method, normalized path template,
stable operation name, execution scope (`global` or `session`), authentication
and role policy, and backend capability name. It contains no FastAPI or
Cloudflare imports, handlers, or storage concerns.

The contract layer compiles templates and validates named path parameters. It
is the sole authority for matching the shared `/api/**` surface, detecting
duplicate method/template pairs, and producing a deterministic route set for
tests and runtime validation.

FastAPI adapts the definitions into `APIRouter` registrations. The Cloudflare
Worker uses the same definitions to authenticate and authorize a request, then
either invokes a global capability or proxies a session-scoped request to the
named Durable Object. The Durable Object resolves the same definition,
requires session scope, verifies that the request's `session_id` identifies
itself, then invokes its local capability.

WebSocket upgrades, SPA/static assets, and runtime bootstrap/health routes
remain native to their runtimes. They are not `RouteDef` entries because they
do not share the same HTTP execution model.

There is no migration dispatcher: the legacy Cloudflare API regexes, prefix
fallbacks, handler lambdas, duplicated method checks, compatibility imports,
and tests coupled to those internals are deleted once their definitions are
served by the contract layer. Existing public endpoint URLs and response
payloads do not change.

For every route applicable to a backend, startup validation requires the
capability declared by its `RouteDef`; absent implementations fail
deterministically rather than silently exposing different behavior. Contract
tests enumerate every definition, assert FastAPI and Cloudflare availability,
and verify method mismatch (405 plus `Allow`), invalid path parameters (422),
authentication (401), and authorization (403) uniformly.

## Security and Authentication

### PAM event authorization

Cloudflare and FastAPI will both expose `POST /api/pam-events` with the same
request validation, session identifier derivation, open/close semantics, and
response payloads. This closes the current feature gap: Cloudflare already
accepts relayed PAM lifecycle notifications, while FastAPI currently supports
only its local listener.

Both endpoints will require a verified principal with session-creation
capability, which is granted to operator and administrator roles by the local
authorization policy. This also supports a machine relay credential expressed
as an appropriately scoped JWT or API-key principal. Viewer credentials will
receive `403`, and unauthenticated callers will receive `401`.

FastAPI's existing local PAM listener remains supported and does not call the
HTTP endpoint when handling events on the same host. The HTTP route is for
remote relays and backend parity; it will create or remove the passive
operator-visible PAM session record directly and will not recursively forward
the event to another relay.

`POST /api/pam-events` is a `RouteDef` global capability in both backends, so
the same declared operator/admin policy is enforced before either handler
runs.

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

Invite redemption is represented as a session/tunnel-scoped capability and is
executed by the owning Durable Object in Cloudflare. The object clears the
invite before returning a bootstrap token, so concurrent redemptions for the
same tunnel cannot both succeed.

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
- The FastAPI and Cloudflare PAM endpoints return matching status codes and
  response schemas for authentication, validation, open, and close cases.
- Consumed or stale invites return the existing invalid/expired response.
- Tunnel protocol selection fails closed when attachment metadata is invalid.
- Short RFB EOF still raises `EOFError`; ordinary fragmentation does not.
- Recording store errors preserve buffered events and are retried on the next
  interval while explicit flush callers still receive the error.
- JWKS refresh errors remain authentication failures and never accept an
  unverified token.
- Shared API route mismatches return `405` with `Allow`; malformed declared
  path parameters return `422`; unknown routes return `404`.

## Testing

Each fix follows a red-green cycle with a focused regression test. Targeted
package suites will run after each subsystem is changed. Final verification
will include:

- `uv run python scripts/run_all_tests.py`
- Go unit tests and the existing targeted race tests
- C# Release build and test suite
- Frontend typechecks and Vitest suites
- Release/package metadata validation
- Shared route-contract unit tests, FastAPI adapter tests, Cloudflare Worker
  and Durable Object adapter tests, and full API parity conformance tests
- A final clean diff and repository status review

## Non-Goals

- No new public authentication mode or credential persistence service.
- No migration aliases or legacy API dispatch path after `RouteDef` adoption.
- No visual redesign of the inspect UI.
- No unrelated cleanup or dependency upgrades.
