# Session Lifecycle Security Conformance Design

## Goal

Close `ARCH-001` with an executable, shared integration-parity gate for the
security-sensitive server lifecycle behavior that is not covered by the
fan-out semantic runner: WebSocket fragmentation, per-principal browser
quotas, governance failure behavior, and resume ownership.

The gate must exercise public HTTP/WebSocket routes on a real configured
server. Unit-only hub/controller tests and source-pattern inventories are not
accepted as parity evidence.

## Contract and status matrix

`spec/session_lifecycle_security_scenarios.json` is the source of truth. Each
scenario declares a category, normalized expected observations, and an exact
status for every implementation:

- `served`: the adapter must execute the scenario and return matching
  observations.
- `unsupported`: the implementation has a server but explicitly refuses or
  does not advertise the surface; the reason and observable refusal are part
  of the contract.
- `unserved`: the package does not mount a server surface; the reason is part
  of the contract.

No backend or required category may be silently absent or skipped. Python,
Go, and C# are served for fragmentation, browser quota, and resume ownership.
Governance records the real configured behavior: Python exercises its signed
webhook path, while Go and C# prove their documented fail-closed public-route
response where the configured governed fan-out surface is unsupported.
Cloudflare and TypeScript are declared per actual mounted surface rather than
being inferred from package-level components.

## Native adapters

`scripts/run_session_lifecycle_security_scenarios.py` owns contract loading,
schema/status validation, process isolation, timeouts, normalized observation
comparison, and the final coverage/cardinality check. It launches native
adapter entry points for Python, Go, and C#.

An adapter may reuse an existing integration fixture, but its scenario path
must:

1. boot the real configured server on an ephemeral listener;
2. use public HTTP/WebSocket routes rather than calling hub/controller methods
   directly;
3. emit one structured observation for the requested scenario;
4. fail if setup is skipped, a route is missing, or the observation cannot be
   produced.

The runner rejects duplicate observations, unknown scenarios/backends,
missing required categories, status drift, successful execution of a declared
unserved cell, and any adapter result that is merely a source/test-name claim.

## Required behavior

### Fragmentation

Drive browser, worker, and tunnel WebSocket routes where served with fragmented
RFC 6455 messages. Observe that no action occurs before the final fragment,
exactly one action occurs after it, and an oversized message is bounded and
refused.

### Browser quota

Start with `max_connections_per_principal = 1`. Observe first admission,
same-subject refusal without disturbing the first connection, successful
admission after disconnect, and rollback after a failed setup.

### Governance

Use configured governance rather than an optional callback left unset. Observe
allow/deny and fail-closed error behavior with zero downstream side effects.
Where a server intentionally does not implement governed fan-out, exercise
the public route and require the documented `501` response and zero delivery.

### Resume ownership

Obtain a browser resume token through the real WebSocket route, establish
ownership, disconnect, and reconnect. Observe restoration only for a current
ownership version, refusal to steal a competing owner, and one-time token
replay refusal.

## Verification and documentation

The central runner and focused conformance tests run in CI alongside the
existing fan-out semantic runner. The protocol/security matrices identify
served, unsupported, and unserved cells using the same vocabulary as the
contract. `ARCH-001` closes only after the native adapters, shared runner, full
relevant language suites, and an independent review pass.
