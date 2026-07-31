# Fan-out Security Corrections Design

## Status and scope

This design supersedes the fan-out portions of the 2026-07-31 remediation
design where the independent review found the prior implementation unsafe.
`FANOUT-001` through `FANOUT-005` remain in progress until implementation,
full verification, and independent spec and quality approval are complete.

The correction covers Python, Go, C#, and the unserved TypeScript component:

- authentication and global-admin authorization at every fan-out surface;
- a controller-owned authorization invariant for every execution path;
- truthful policy outcomes and caller identity;
- output capture before dispatch;
- immutable, atomic Go and C# group storage;
- a total C# dispatch-and-collection deadline; and
- executable shared semantic conformance evidence.

It does not add C# browser-WebSocket fan-out or mount TypeScript fan-out in the
Node server. Those remain unsupported and must not be advertised.

## Security boundary

Fan-out is a global-admin operation. Every one of the five REST operations —
create, list, delete, send, and grant — requires an authenticated global admin.
Python and Go browser-WebSocket `fanout_send` requires the same global-admin
decision. A session-scoped admin, group creator, group grantee, operator, or
viewer is not sufficient.

Transport gates are defense in depth, not the authoritative boundary. Each
public controller send accepts a real principal object and uses a mandatory,
injected authorizer with these operations:

1. decide whether the principal is a global admin;
2. resolve every member from the stored group at execution time; and
3. decide whether that principal may currently read each resolved session.

Missing principals, missing authorizer dependencies, authorization errors,
unknown members, and revoked access fail closed. Group grants permit discovery
of a group but never substitute for global-admin or session authorization.
Approval release persists the full originating principal and repeats the same
admin and per-member checks.

No public API accepts a caller-supplied authorized member subset. Raw subset
dispatch is private/internal and receives only the controller-produced snapshot
of stored members. This removes the confused-deputy path where a caller could
add an arbitrary worker ID or invoke the controller without session authz.

Python and TypeScript use their existing full principal models. Go and C# add
small fan-out authorizer adapters over the existing server registry and authz
services; the fan-out controller owns those adapters and refuses execution
when they are absent.

## Policy identity and result contract

Policy context uses the caller's actual strongest normalized role, ordered
`admin`, `operator`, `viewer`; no controller hardcodes `admin`. The context also
retains the subject and fan-out group metadata. Since transport and controller
authorization run first, a non-admin caller never reaches the policy gate.

Python REST, Python WebSocket, and the TypeScript route module use one canonical
result shape. The existing HTTP success status remains 200 for compatibility,
but every result serializes:

- `error`: string or `null`;
- `approval_required`: boolean; and
- `approval_id`: string or `null`.

Normal execution has `error = null`, `approval_required = false`, and
`approval_id = null`. Policy denial has a non-empty `error` and no delivery.
Policy hold has `approval_required = true`, a non-empty `approval_id`, and no
delivery. The WebSocket result adds only its `type` discriminator; it does not
drop result fields. A deny or hold can therefore never resemble an empty
successful send.

## Output capture lifecycle

Output subscription is a preparation step, not part of post-send collection.
Python, Go, and TypeScript expose an internal capture handle with three phases:

1. open/subscribe for a worker;
2. collect from the already-open subscription; and
3. close/unsubscribe exactly once.

Parallel mode opens captures for every authorized member before any observer
notification or worker input. Sequential mode opens a member's capture
immediately before notifying and sending to that member. A capture that cannot
be opened is a failed member and receives no input. Rejected sends, exceptions,
cancellation, and policy refusal all close prepared captures.

Elapsed response time begins at accepted worker dispatch, not during capture
preparation or observer notification. A deterministic test hub emits output
synchronously inside its send operation; that output must be returned. Tests
must not wait for subscriber counts or insert sleeps to hide subscription order.

C# already subscribes before dispatch and retains that behavior.

## Group-store isolation and atomic mutation

Go and C# in-memory stores never retain or return caller-owned mutable group
objects. Save, get, and list operations deep-clone group records, including
worker ID and grant collections.

Grant mutation becomes a domain operation on the store. Creator validation,
duplicate detection, mutation, and persistence occur under one store lock.
Controller code does not perform get-mutate-save on a live object. Tests cover:

- mutating the object passed to save;
- mutating an object returned by get or list;
- concurrent distinct grants without lost updates; and
- concurrent enumeration while grants change.

Go runs the store/controller race suite with `-race`.

## C# operation deadline

C# interprets `maxResponseMs` as one total bound for observer notification,
worker dispatch, and collection for the whole group operation. One monotonic
deadline and linked cancellation source are created before dispatch. Every
stage receives the remaining budget.

The controller stops awaiting a non-cooperative broadcast or worker send when
the deadline expires, marks unfinished members failed, and returns within the
bound. Underlying tasks that ignore cancellation are retained and observed so
late faults cannot become unobserved task exceptions. Caller cancellation is
not converted into a timeout result; it propagates as cancellation.

Parallel work shares one deadline. Sequential mode does not restart the full
budget for each member. A deterministic hanging-send test asserts bounded
return, failure reporting, cancellation request, and observation of a later
fault.

## Executable semantic conformance

The declaration-only `spec/fanout_security_coverage.json` and its regex
validator are replaced by `spec/fanout_security_scenarios.json`. Each scenario
describes semantic inputs and expected outcomes rather than a test name:

- actor authentication state, roles, and session scope;
- group members and optional dormant-member configuration;
- current session visibility and an optional authorization mutation;
- optional group grant;
- policy action (`allow`, `deny`, or `hold`) and optional approval release;
- worker acceptance/failure and immediate output; and
- expected transport status, result fields, delivered workers, observer
  notifications, failed members, and captured output.

The required scenario set covers unauthenticated refusal, viewer refusal on a
public session, strict and permissive dormant admission, authorization
revocation, grant non-bypass, partial member failure, policy denial, policy
hold/release, direct-controller missing dependencies, immediate output, store
isolation/atomicity where applicable, and the C# operation deadline.

Every scenario declares one status per backend/surface:

- `execute`: the backend adapter must execute and report the normalized result;
- `unsupported_fail_closed`: the adapter must execute the explicit refusal and
  prove no input;
- `component_execute`: the unserved TypeScript component executes the semantic
  case without claiming a server surface; or
- `unserved`: no server cell may be advertised or run.

Each language owns one semantic adapter test that loads the shared file,
executes every applicable scenario through real controller/route boundaries,
and asserts that its observed scenario IDs exactly equal the applicable IDs.
The adapters emit normalized JSON results.

`scripts/run_fanout_security_scenarios.py` invokes the Python, Go, C#, and
TypeScript semantic suites, reads their normalized output, compares it with the
shared expectations, and fails on a missing ID, unexpected skip, false
capability claim, unsupported-status mismatch, or command failure. Static test
declaration regexes are not evidence.

The live strict-admission scenario remains. The live matrix additionally
intersects each scenario's requirements with both client and announced server
capabilities before starting a cell, so an unserved TypeScript server is
reported unsupported even when manually selected.

## Error handling and compatibility

- Unauthenticated REST calls return 401 before body parsing or resource lookup.
- Authenticated non-admin calls return 403 before group/session disclosure.
- Controller authorization dependency failure returns a typed fan-out error and
  produces no worker input or observer notification.
- Unknown/revoked members remain per-member failures for an otherwise permitted
  send.
- Go and C# configured policy remains explicitly unsupported/fail-closed with
  no delivery; Python policy deny/hold/release and TypeScript component policy
  behavior remain implemented.
- TypeScript server fan-out remains unserved and unadvertised.

The documentation that previously described operator fan-out group creation is
updated to the global-admin contract.

## Verification and completion gate

Every production change follows a witnessed red-green cycle. Verification
includes focused tests, Python Ruff, Go tests/vet/race, C# serial tests/build,
TypeScript tests/typecheck/lint, the semantic scenario runner, and the live
matrix.

`FANOUT-001` through `FANOUT-005` remain `[~]` after implementation and local
verification. They return to `[x]` only after independent spec-compliance and
code-quality reviewers approve the complete correction with no open findings.
