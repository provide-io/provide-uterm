# uterm Semantic and Safety Convergence Design

## Purpose

Define executable cross-language contracts and repair the remaining correctness
and security defects in the C#, Go, and native implementations. Existing Python
behavior is the starting oracle, while shared fixtures become the final authority.

## Executable semantic evidence

Shared JSON fixtures describe inputs and expected observations without embedding
language-specific names. A scenario observation contains a stable scenario ID,
backend ID, support classification, result or error code, state before and after,
and behavior-specific evidence. Central runners validate schemas, execute each
native adapter under a deadline, reject duplicate or missing scenarios, and
compare results.

The initial additions cover:

- transaction isolation, read-your-writes, rollback, conflict, and reaping;
- annotation one-shot and arbitrarily chunked streaming matches;
- WebSocket message-size boundaries and fragmented over-limit messages;
- secure append through ordinary files, symlinks, swaps, and permission changes;
- Unix, abstract-Unix, IPv4, IPv6, truncated, and unnamed socket addresses; and
- route/capability inventory consistency.

Generated tests may adapt fixtures to native test frameworks, but placeholder
assertions and source-text-only evidence are forbidden.

## C# control-plane transactions

### Public contract

Every control-plane store operation accepts an `ITx` owned by the same `IEngine`.
The engine rejects transactions from another engine, completed transactions, and
uses after disposal. A transaction supports commit and rollback exactly once.
Compile-time call-site failures drive the migration of the graphical target
registry and all tests.

### In-memory engine

`Begin()` creates a transaction-local deep snapshot and working set. Records are
deep-cloned at transaction and store boundaries because the current record types
are mutable reference types. Reads observe transaction-local writes; rollback
publishes nothing.

Commit validates the engine revision captured at begin. If the revision changed,
commit fails with a stable conflict error and publishes nothing. Otherwise the
entire working state is atomically installed and the revision advances. This is
optimistic serializable behavior and prevents lost updates without holding a
global lock for arbitrary caller work.

### SQLite engine

`Begin()` returns a `SqliteTx` wrapping one connection transaction. Store commands
receive that transaction explicitly and bind both its connection and transaction.
The ambient `_current` slot is removed, so unrelated concurrent requests cannot
join another caller's transaction. Engine ownership and completed-state checks
match the memory engine.

### Reaping

Both engines implement the same time-based cleanup in one transaction. Reaping
removes expired sessions, expired or revoked session/resume tokens, expired or
deleted leases, and resolved approvals outside their retention window. Boundary
timestamps are fixed by fixtures and cleanup is idempotent.

## C# annotation detector

The C# detector consumes the canonical rule inventory and event vocabulary used
by the Python annotation package. If direct generation is practical, a committed
language-neutral rules artifact is generated from one source of truth; otherwise
the conformance runner enforces exact rule parity.

Streaming feed combines the previous unmatched suffix with the new chunk, emits
each newly completed match once, and retains only the suffix capable of beginning
a future match. The retained suffix is bounded by the longest rule requirement.
One-shot, byte-at-a-time, split-at-every-position, overlap, and repeated-command
fixtures must yield identical annotations and no duplicate emissions.

## Bounded Go WebSocket input

Gateway, control client, WebSocket transport, tunnel client, and watch paths use a
shared bounded message reader. The server-aligned default is one MiB unless an
existing protocol-specific lower bound applies. Public client configuration may
raise the bound explicitly within a validated positive range.

The reader accumulates fragments up to the configured limit, rejects an over-limit
message before unbounded allocation, reports one stable error, and closes or
terminates the affected protocol loop. Exact-limit messages succeed. Zero,
negative, overflowed, and nonsensical configuration is rejected at construction.

## Descriptor-safe C# append

On Unix, secure append opens the target with no-follow and close-on-exec semantics,
then validates the opened descriptor with `fstat`, applies permissions with
`fchmod`, and wraps that same descriptor. It never authorizes one path object and
opens or chmods another. Non-regular files and links fail closed.

On Windows, the implementation opens a non-reparse-point handle with non-inheritable
sharing semantics, validates the handle, and applies the documented ACL to that
handle or fails explicitly when the platform cannot guarantee it. Tests exercise
the real platform implementation; unsupported guarantees are not simulated as
success.

## Native capture address formatting

Socket-address formatting accepts the actual address length supplied by the hook.
For Unix sockets it calculates the available `sun_path` byte count using
`offsetof`, never scans beyond it, and distinguishes pathname, abstract, unnamed,
and truncated addresses. It escapes or length-bounds non-text bytes so logging
cannot read adjacent memory.

IPv4 and IPv6 formatting uses reentrant conversion into caller-owned buffers.
Linux and macOS share golden cases where structure layout permits and retain
platform-specific guards where it does not. Unit tests run under the native test
target; sanitizer builds exercise truncated and non-terminated inputs.

## Testing and rollout

Each defect starts with a focused failing test. Cross-language fixtures are added
before implementation when they define new shared behavior. Package tests run
after each bounded change, followed by C# coverage, Go race, and native sanitizer
checks.

The C# API break ships atomically with all repository call sites migrated. No
compatibility overload retains ambient behavior. Documentation calls out the
pre-1.0 migration and shows the begin/use/commit pattern.
