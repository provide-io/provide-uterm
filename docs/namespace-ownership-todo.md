# Namespace Ownership Todo

Date: 2026-05-28

## Goal

Resolve the duplicate `provide.uterm.bridge` package ownership so imports are deterministic, type checkers can reason about package boundaries, and long-term developer experience is clear.

## Recommended Long-Term Shape

Make `provide.uterm.bridge` the stable, lightweight user-facing bridge API owned by `provide-uterm`, and move server runtime internals to `provide.uterm.server.bridge`.

Target imports:

```python
# Lightweight, safe in core-only installs.
from provide.uterm.bridge import HijackableMixin, HijackCoordinator
from provide.uterm.bridge.schemas import BrowserFrame, WorkerFrame

# Runtime/server implementation, requires provide-uterm-server.
from provide.uterm.server.bridge import TermHub
from provide.uterm.server.bridge.routes import register_ws_routes
```

## Rationale

- Core APIs live in `provide-uterm`; hosted runtime code lives in `provide-uterm-server`.
- Users get simple, stable imports for worker-facing bridge primitives.
- Maintainers get explicit imports for server runtime code.
- Package installation order no longer changes `import provide.uterm.bridge` behavior.
- Static type checkers no longer see duplicate concrete modules for `provide.uterm.bridge`.

## Implementation Notes

1. Move pure bridge primitives into `provide-uterm`:
   - `HijackableMixin`
   - coordinator/contracts/schemas
   - identity/models only if they are transport-independent and do not pull server dependencies

2. Move runtime bridge code under `provide.uterm.server.bridge`:
   - hub
   - websocket/rest routes
   - worker link
   - fanout/runtime stores if they are server-bound

3. Do not keep compatibility shims for old server paths:
   - `provide.uterm.server.bridge.hub`
   - `provide.uterm.server.bridge.routes`
   - other server-owned bridge modules currently imported by users or tests

4. Remove the duplicate server-owned `provide/uterm/bridge/__init__.py`.

5. Add helpful migration errors for moved package-level server names where practical:

```text
TermHub moved to provide.uterm.server.bridge.TermHub
```

6. Update tests, docs, package data, mypy config, and import snapshots to reflect the new ownership.

## Open Decisions Before Implementation

- Compatibility window: immediate breaking cleanup.
- Package-level compatibility: immediate break. Do not lazily re-export moved server symbols from `provide.uterm.bridge`; use explicit new imports and clear error messages where practical.
- Exact list of bridge modules considered pure core API versus server runtime API.
- Dynamic split-shim policy: remove runtime `globals()` export shims. Replace them with explicit imports/re-exports or direct public modules so mypy and ty can see the public API.

## Related Review Decisions

- SSH gateway defaults: require explicit opt-in for public unauthenticated listeners. Default to loopback-only or require authenticated configuration for non-loopback binds. Public unauthenticated behavior should require an explicit flag/config such as `--allow-unauthenticated` in combination with an explicit public bind.
- Tunnel token transport: API and WebSocket auth should be cookie-only. Do not keep query-string bearer tokens as a normal auth transport.
- Share-link bootstrap: implement it. Use a short-lived one-time invite code in the URL, exchange it for an HttpOnly cookie on first request, immediately redirect to a clean URL, then revoke or expire the code.
- Invite-code semantics: use `?invite=...`, 60-300 second TTL, single-use consumption, clean-url redirect after cookie set, and separate invite scopes/classes for share versus control links.
- Type checking: make both mypy and ty clean where practical. Prefer package-isolated checks during migration; after namespace cleanup, add an official all-package gate.
- Official verification command: add `make quality` as the canonical all-package quality gate. It should run Python package tests with isolated coverage contexts, ruff, mypy, ty, npm typecheck, npm lint, and npm tests.
- Generated frontend assets: follow build-artifact best practice. Do not accumulate stale hashed assets in source-controlled server package output. Clean generated output before build or build into a generated/dist directory and include deterministic assets only in build/release artifacts.
- Control-plane memory backend: fix local transaction isolation first with locking/staged commits before changing the default backend.
