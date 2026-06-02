# provide-uterm Code Review & Architecture Analysis - Part 1: Core Bridge System (TermHub)

This section of the code review focuses on the Core Bridge System, analyzing the `TermHub` composition (Hub Services) and the `WorkerLink` bridge mechanism. We evaluate the codebase against four critical lenses: Architecture, Maintainability, Security & Concurrency, and Performance & Scaling.

### 1. Architecture & General Health (Data Flow, Composition)

**Findings:**
- **Service Composition:** `TermHub` (`core_impl.py`) acts as an effective facade for nine dedicated service classes: `WorkerRegistry`, `RateLimiter`, `InMemoryApprovalStore`, `HijackLeaseManager`, `MessageRouter`, `ConnectionManager`, `PresenceManager`, `StateStore`, and `PollingCoordinator`. This clarifies domain boundaries.
- **WorkerLink Bridge:** The worker-side `TermBridge` (`worker_link.py`) is well-structured. It handles reconnections using exponential backoff, drops or rejects invalid payloads appropriately, and safely interfaces with the PTY/session in a non-blocking manner.
- **Data Flow:** Frame routing cleanly distinguishes between `term` (raw bytes from PTY to browsers) and `control` frames. The introduction of `is_tunnel_worker` flags allows raw byte multiplexing without JSON envelope overhead, which is excellent for raw PTY performance.

### 2. Maintainability & Structural Design

While the transition to composed classes is a massive leap forward for maintainability, some architectural coupling remains.

**Findings:**
- **Circular Dependencies / Back-references:** Several composed services rely on "reaching back" into the `TermHub` facade. For example, `MessageRouter` is constructed as `self.router = MessageRouter(self)` and accesses `hub._lock`, `hub.registry`, and `hub.prepare_policy_context`. This creates bidirectional coupling, where the managers are inextricably bound to the specific `TermHub` implementation rather than generic interfaces.
- **Protocol Usage:** `HijackLeaseManager` mitigates this slightly by defining `_LeaseHubCallbacks(Protocol)` for its hub reference. This is a very robust Pythonic pattern that should be replicated across other managers (`MessageRouter`, `ConnectionManager`, etc.) to fully decouple them and make them unit-testable in isolation.
- **Back-compatibility Shims:** `TermHub` still contains numerous back-compatibility shims (`@property` accessors for `_workers`, `_rest_acquire_bucket`, etc.). While necessary for the phased refactor, these should be tracked as technical debt and progressively stripped out to finalize the Phase 7 decoupling.

### 3. Security & Concurrency Robustness

The codebase uses a single, coarse-grained asynchronous lock (`TermHub._lock`) to protect concurrent state mutations. This generally avoids race conditions but has introduced a critical concurrency anti-pattern.

**Critical Findings:**
- **I/O Under Lock in `try_acquire_rest`:** In `lease.py`, `HijackLeaseManager.try_acquire_rest` performs network I/O while holding the global hub lock:
  ```python
  async with self._lock:
      ...
      # Send pause while holding the lock to ensure the worker is notified
      # atomically with the session creation.
      await st.worker_ws.send_text(...)
      ...
  ```
  **Risk:** If the worker's WebSocket write buffer is full (TCP backpressure) or the connection is slow, `send_text` will yield and block. Because the single global `TermHub._lock` is held, *all* other workers, browsers, and API requests across the entire server will stall waiting for the lock. This is a severe Denial of Service (DoS) vulnerability and performance hazard.
  **Recommendation:** The network I/O must be moved outside the lock. You can achieve atomicity by marking the state as "pending_hijack" or similar under the lock, releasing the lock, performing the I/O, and then re-acquiring the lock to finalize the `HijackSession`.

- **Redaction & Policies:** The `MessageRouter` cleanly enforces output redaction via `_output_policy_gate` before broadcasting. The application of redaction to both the broadcast path and the persistent ring buffer (`_redact_event_payload`) ensures secrets do not leak via the `events/watch` API.

### 4. Performance & Scaling

The system is designed to support up to 10,000 workers (`max_workers = 10000`). Under this load, several scaling bottlenecks emerge.

**Findings:**
- **Global Lock Contention:** As mentioned, `TermHub` relies on a single `asyncio.Lock()` for all workers. At 10,000 workers, concurrent connections, broadcasts, and state updates will cause intense contention on this single lock.
  **Recommendation:** Shard the lock. The lock should be granular, ideally one `asyncio.Lock` per `WorkerTermState` (or per `worker_id`). The global lock should only protect the `WorkerRegistry`'s internal dictionary mutations (adding/removing a worker), while per-worker locks protect the individual worker's leases, browsers, and state.
- **Sequential Broadcasts:** In `router_impl.py`, `MessageRouter.broadcast` iterates over `browsers_with_roles` and awaits each `send_text` sequentially:
  ```python
  for ws, role in browsers_with_roles:
      ...
      await asyncio.wait_for(ws.send_text(final_payload), timeout=_BROADCAST_SEND_TIMEOUT_S)
  ```
  **Risk:** If there are 50 browsers watching a session, and the first browser is slow (taking 4 seconds to timeout), the 50th browser receives the frame 4 seconds late.
  **Recommendation:** Fan-out the broadcasts concurrently using `asyncio.gather(*tasks, return_exceptions=True)` or `asyncio.TaskGroup`.
- **Memory Footprint:** The `WorkerTermState` properly limits memory growth by restricting the event ring buffer to `event_deque_maxlen` (default 2000) and aggressively capping the payload size of `term` events (`max_event_data_chars`, default 8192). This is a safe and robust strategy for long-running sessions.

## Part 2: Server Transports & Protocol Gateways

This section of the code review focuses on the Server Transports & Protocol Gateways, analyzing the gateway layer located in `gateway/` (including `_gateway.py`, `_ssh_gateway.py`, `_ssh_handler.py`, `_telnet_gateway.py`, and `_iac_negotiate.py`). We evaluate the codebase against four critical lenses: Architecture, Maintainability, Security & Concurrency, and Performance & Scaling.

### 1. Architecture & General Health (Data Flow, Composition)

The gateway layer acts as a protocol bridge, translating legacy text-based protocols (SSH, Telnet) into the native WebSocket frames expected by the uterm terminal server.

**Findings:**
- **Pluggability & Separation of Concerns:** The architecture successfully isolates the transport specifics from the core gateway logic. `_ssh_gateway.py` and `_telnet_gateway.py` handle connection lifecycle and protocol-specific listeners, while `_gateway.py` acts as the unifying core, supplying bidirectional data pumps (`_tcp_to_ws`, `_ws_to_tcp`) and handling the session-resume control plane.
- **IAC Negotiation State Machine:** Telnet negotiation is delegated to `_iac_negotiate.py`, which provides a stateful `IacNegotiator` to perform RFC 1091 (TTYPE) and RFC 1572 (NEW-ENVIRON) handshakes. This is a robust approach, stripping IAC noise while capturing terminal color hints (`?colormode=...`) without blocking.
- **Resilience:** Both the Telnet and SSH gateways feature automated, token-based reconnection mechanisms. If the upstream WebSocket drops (e.g., due to load-balancer timeout or hibernation), the gateway transparently reconnects using the cached `session_token` with an exponential backoff of up to 12 attempts with a 3.0-second delay. This guarantees high availability for legacy clients.

### 2. Maintainability & Structural Design

The gateway codebase has been modularized to avoid monolithic files, but some complexity remains in the orchestration of tasks.

**Findings:**
- **Module Sizing:** The split between `_gateway.py` (core pumps), `_ssh_gateway.py` (listener), and `_ssh_handler.py` (process handler) was explicitly done to keep files under a 500-line budget. This constraint ensures files are readable and maintainable.
- **SSH Abstraction Complexity:** In `_ssh_handler.py`, extracting the client's public key fingerprint is complex. `_make_process_handler` attempts to traverse multiple internal `asyncssh` private attributes (`_chan._conn`, `_owner`, etc.) to find the accepted client key.
  **Risk:** This reliance on undocumented internals is brittle and could break silently during future `asyncssh` version upgrades, leading to missing client key metrics or token routing issues.
  **Recommendation:** Upstream a patch or feature request to `asyncssh` to expose the accepted public key cleanly via the `SSHServerProcess` or `get_extra_info`. In the interim, encapsulate this lookup into a single, well-tested utility function with fallback logging so any upstream breaks are loudly reported.
- **Helper Encapsulation:** Shared constants and utility functions like `_read_token` and `_normalize_crlf` are well isolated, though they remain private to `_gateway.py`.

### 3. Security & Concurrency Robustness

The gateways handle untrusted legacy protocols, demanding strict security and safe concurrency primitives.

**Findings:**
- **Authentication Handshake:** The SSH gateway (`_ssh_gateway.py`) properly validates public keys using the pluggable `SSHKeyResolver`. Unauthenticated connections are strictly prevented from binding to non-loopback addresses unless explicitly configured with `allow_unauthenticated=True`.
- **Token Persistence Security:** Token files stored on disk via `token_file` are safely written with 0600 file permissions and 0700 parent directory permissions in `_gateway.py` (`_write_token`). In `_ssh_handler.py`, tokens are isolated into per-user files derived from the SHA256 fingerprint of the client's public key, preventing multiple users from fighting over the same resume token.
- **Concurrency & Resource Leaks:** The gateway handles bidirectional forwarding by spawning two concurrent `asyncio.Task`s (`t1`, `t2`) and waiting for them with `asyncio.FIRST_COMPLETED`. It correctly guarantees the cancellation of the pending task, preventing orphaned tasks and memory leaks upon connection closure.

### 4. Performance & Scaling

The gateway operates as a man-in-the-middle for potentially high-throughput data streams.

**Findings:**
- **Backpressure Handling:** The `_ws_to_tcp` and `_tcp_to_ws` pumps in `_gateway.py` respect backpressure. They use `await writer.drain()` after writing to TCP streams, ensuring memory doesn't balloon if the legacy client reads slowly. The read chunk size is capped at a conservative 4096 bytes (`reader.read(4096)`), maintaining a low per-connection memory footprint.
- **Subnegotiation Buffer Bounds:** `IacNegotiator` (`_iac_negotiate.py`) limits its internal subnegotiation buffer (`_sb_buf`) to `_MAX_SB_BYTES` (4096 bytes). This bounds the memory risk from a malicious or buggy Telnet client that sends an `IAC SB` but never closes it with an `IAC SE`.
- **String Manipulations:** In `_gateway.py`, functions like `_normalize_crlf` execute multiple `.replace()` calls on byte payloads (e.g., `raw.replace(b"\x7f", b"\x08")`).
  **Risk:** While simple, these operations create intermediate byte objects on every chunk. In extreme high-throughput scenarios, this could increase garbage collection overhead.
  **Recommendation:** If profiling indicates this is a hot path under heavy load, replace the chained byte replacements with a single pass using Python's `bytes.translate()` method, which maps bytes directly in C and avoids allocating intermediate strings.

## Part 3: Cloudflare Workers & Edge Architecture

This section of the code review focuses on the Cloudflare Workers architecture and Durable Objects layer, analyzing the edge state modeling and hibernation efficiency. We evaluate the codebase against four critical lenses: Architecture & Health, Maintainability & Structure, Security & Concurrency, and Performance & Scaling.

### 1. Architecture & General Health (Data Flow, Composition)

**Findings:**
- **Durable Object State Modeling:** The `SessionRuntime` Durable Object acts as a stateful coordinator for terminal sessions. The architecture leverages mixins (`_AuthMixin`, `_FetchMixin`, `_LifecycleMixin`, `_SessionRuntimeIoMixin`, `_WsHelperMixin`) to compose the `SessionRuntime` (`runtime.py`), effectively avoiding a monolith. `SessionRuntime` persists session metadata and terminal buffers into an embedded SQLite database (`SqliteStateStore`), which allows it to recover gracefully after Cloudflare hibernates the DO.
- **WebSocket Hibernation Efficiency:** The application is explicitly designed for Cloudflare's WebSocket hibernation. In `lifecycle.py`, methods like `webSocketOpen` and `webSocketClose` handle connection events independently of the `fetch()` handler. The use of SQLite to persist the resume token (`_open_resume_token`) ensures browsers can securely reclaim connections when the DO wakes from hibernation without requiring re-authentication.

### 2. Maintainability & Structural Design

**Findings:**
- **SQLite State Migration & Schema:** The `SessionRuntime` calls `self.store.migrate()` in its `__init__` (in `runtime.py`). This guarantees the SQLite schema is always up-to-date when the isolate spins up.
- **Mixin Pattern:** While the mixin pattern splits code by domain, it creates implicitly coupled namespaces where a mixin expects certain attributes to exist on `self` (e.g., `_LifecycleMixin` requires `self.worker_ws`, `self._send_text`, `self._remove_ws`, etc., forcing an extensive set of `TYPE_CHECKING` stubs).
  **Risk:** This "god object" via composition makes it hard to test the mixins in isolation, as they require a mocked DO context with dozens of attributes.
  **Recommendation:** Instead of mixins, use composition via delegated helper classes. Pass the `SessionRuntime` reference or specific interfaces to these helpers.

### 3. Security & Concurrency Robustness

**Findings:**
- **KV Eventual Consistency & Credentials:** `registry.py` handles the fleet-wide session registry via Cloudflare KV (`SESSION_REGISTRY`). Because KV is eventually consistent, there is a risk of staleness. However, in `runtime.py`, `_ensure_credentials` correctly manages this by fetching credential hashes with a short TTL (`_CREDENTIAL_TTL_S = 60.0`). It intelligently handles a "transiently missing" KV entry (a `None` result) by keeping the last known hashes, explicitly avoiding a false revocation.
  **Risk:** A true token revocation could be delayed up to 60 seconds (the `_CREDENTIAL_TTL_S` cache window), allowing an evicted client up to 60 seconds of continued access.
  **Recommendation:** If strict token revocation is required instantly, the auth gateway or DO should have an out-of-band invalidation mechanism (e.g., a pub/sub event) rather than purely polling KV.
- **Lease Expiration Persistence:** In `persistence.py`, `persist_lease` converts the monotonic `lease_expires_at` timestamp to a wall-clock timestamp before saving to SQLite. This prevents a critical security bug where a hibernated DO wakes up with a reset monotonic clock, potentially extending a hijack lease indefinitely.

### 4. Performance & Scaling

**Findings:**
- **ushell Serverless Terminals:** `ushell.py` enables "serverless" terminals directly inside the Durable Object. Instead of proxying traffic to a backend Python worker over `worker_ws`, it loads `UshellConnector` into memory on the edge and intercepts inputs via `_ushell.handle_input`. This provides sub-millisecond latency for edge shell execution.
- **Payload Proxy Overhead:** In `lifecycle.py`, WebSocket messages are routed based on role. `webSocketMessage` correctly uses `to_py()` or `to_bytes()` to convert Pyodide's `JsProxy` ArrayBuffer objects into Python byte arrays before routing them to `handle_tunnel_message`.
  **Risk:** Pyodide data proxying across the JS/Python boundary is expensive. Large binary transfers (e.g., tunnel multiplexing) converting `JsProxy` memoryviews on every chunk will consume significant CPU time within the 50ms edge CPU limit.
  **Recommendation:** Offload large binary frame forwarding directly to JavaScript handlers where possible, bypassing the Pyodide translation layer, or increase the chunk size to minimize boundary crossing frequency.

## Part 4: Frontend Application & xterm.js Integration

This section of the code review focuses on the Frontend Application located in `packages/provide-uterm-frontend/`. We evaluate the separation between raw `xterm.js` rendering and orchestration logic, state synchronization for the 'DeckMux' (collaborative presence/cursors), and rendering performance for high-throughput streams. We analyze the codebase against four critical lenses: Architecture & Health, Maintainability & Structure, Security & Concurrency, and Performance & Scaling.

### 1. Architecture & General Health (Data Flow, Composition)

**Findings:**
- **Separation of Concerns:** The architecture clearly demarcates raw terminal rendering from orchestration. `ProvideTerminal` (`terminal_impl.ts`) exclusively manages DOM construction, `xterm.js` lifecycle, and a streamlined WebSocket connection, ensuring single-agent output is minimally obstructed.
- **DeckMux Orchestration:** Collaborative logic and presence multiplexing ('DeckMux') are cleanly isolated in `ProvideHijack` (`hijack_impl.ts`). It supervisors the terminal and provides advanced hijack controls (pause/step/release) and presence indicators without polluting the core rendering pipeline.
- **WebSocket Lifecycle Extraction:** The `ProvideHijack` WebSocket connection logic and exponential backoff are cleanly extracted into free functions in `hijack-websocket.ts`, preventing the DOM/UI components from being overly bloated.

### 2. Maintainability & Structural Design

**Findings:**
- **Control Channel Decoding:** `ProvideTerminal` and `ProvideHijack` both utilize `ControlChannelDecoder` (`hijack-codec.ts`) to intercept and parse inline JSON control frames from the WebSocket stream. The decoder enforces limits (`_DEFAULT_MAX_CONTROL_BYTES = 1024 * 1024`, `_DEFAULT_MAX_CONTROL_DEPTH = 32`) preventing malicious or malformed nested payloads from causing stack overflows or high memory usage.
- **File Structure:** The modular split—keeping UI logic (`hijack_impl.ts`, `hijack-ui.ts`), state management (`hijack-state.ts`), WebSocket management (`hijack-websocket.ts`), and codec logic (`hijack-codec.ts`) strictly separated—is excellent for maintainability.

### 3. Security & Concurrency Robustness

**Findings:**
- **DeckMux State Synchronization:** The frontend maintains a dedicated `HijackState` container separate from the DOM. Server-confirmed roles are preferred over constructor inputs (`this._state.serverRole ?? this._config.role`), which safely anchors UX decisions (like admin approval modals versus statusbars) in server authority rather than client claims.
- **Robust Resumption:** `sessionStorage` is utilized to persist a `resumeToken`. Upon WebSocket disconnection, `hijack-websocket.ts` explicitly avoids clearing the `resumeToken`, guaranteeing successful session resumption on exponential backoff reconnects (`delaySec` scaling up to 30s) without dropping terminal context.
- **Approval Expiry:** When handling `approval_pending` frames, the UI calculates remaining seconds (`computeRemainingSeconds`) and removes the approval UI automatically when the timer reaches zero, avoiding dangling interactive elements for expired control requests.

### 4. Performance & Scaling

**Findings:**
- **Control Framing Efficiency:** The `ControlChannelDecoder` buffers UTF-8 string chunks without allocating new strings for every frame until a control chunk is recognized via `_DLE` and `_STX` delimiters.
  **Risk:** The current string-concatenation approach `this._buffer += String(chunk ?? "")` can degrade under exceptionally high-throughput streams since strings are immutable and large concats create garbage collection pressure.
  **Recommendation:** Under extreme load, migrating the control frame codec from a pure string buffer to an ArrayBuffer / Uint8Array approach can significantly improve efficiency by reducing allocation frequency.
- **Rendering Throughput Fallback:** In `ProvideTerminal`, when `ControlChannelDecoder` encounters a stream corruption (`try-catch` around `this.wsDecoder.feed(payload)`), it proactively resets the decoder and writes the raw payload directly to `term.write()`.
  **Risk:** While this correctly prevents users from staring at a blank screen when control frames get garbled, it abandons control flow on corrupt streams.
  **Recommendation:** Log the fallback event or alert the user subtly, and optionally attempt to recover the control frame boundary using `_DLE` scans rather than completely falling back to pure data passthrough.
- **Scrollback Buffer Reset:** When receiving a `snapshot` frame, the frontend carefully uses ANSI soft reset and clear screen (`t.write(" \x1b[!p \x1b[2J \x1b[H")`) instead of `t.reset()`. This intelligently preserves the scrollback buffer and avoids breaking scroll indicators, enhancing UX during state resyncs.

## Part 5: AI & MCP Tooling Integration

This section of the code review focuses on the AI and MCP tooling integration located in `packages/provide-uterm-client/src/provide/uterm/ai`. We evaluate the codebase against four critical lenses: Architecture & Health, Maintainability & Structure, Security & Concurrency, and Performance & Scaling.

### 1. Architecture & General Health (Data Flow, Composition)

**Findings:**
- **FastMCP Tool Surface:** The `server_impl.py` module exposes 21 explicit FastMCP tools covering session management, hijack lifecycle, and worker control. This establishes clear and comprehensive boundaries for AI agents to interact with terminal sessions.
- **Context Scoping:** The `AuthorizationContext` safely encapsulates the `McpPrincipal` (resolved either via request-scoped state or transport headers). This enables legacy callers using `X-Uterm-Principal` headers to function correctly while providing a secure fallback behavior for local `stdio` agents.

### 2. Maintainability & Structural Design

**Findings:**
- **Centralized Authorization Policy:** `policy.py` acts as a single source of truth for tool-to-role mappings (`viewer`, `operator`, `admin`). The `@authorized` decorator in `auth.py` acts as a robust chokepoint. If a new tool is added to `server_impl.py` without a corresponding entry in `TOOL_REQUIRED_ROLES`, the `required_role` function safely raises a `KeyError`, preventing unguarded endpoints.
- **Centralized Constants:** `constants.py` centralizes input-hardening limits such as `MAX_KEYSTROKE_BYTES` (4096) and `MAX_USER_PATTERN_LEN` (512). This avoids inline magic numbers and guarantees that client and server sanitization parameters remain in sync.

### 3. Security & Concurrency Robustness

**Findings:**
- **Regex Denial of Service (ReDoS):** The `_compile_user_pattern` function relies on a length cap (`MAX_USER_PATTERN_LEN = 512`) to prevent amplification, but compiles the pattern using the standard Python `re` module.
  **Risk:** A 512-character limit is large enough for a malicious or hallucinating LLM to construct short, pathological patterns that cause catastrophic backtracking in the `re` engine, leading to a CPU exhaustion Denial of Service (DoS).
  **Recommendation:** Migrate from the standard `re` module to a linear-time engine like `google-re2` for evaluating untrusted/LLM-supplied regex patterns.
- **Server-Side Request Forgery (SSRF) and DNS Rebinding:** In `session_create`, `_validate_session_create_config` evaluates hostnames against `ALLOW_PRIVATE_HOSTS` and a hardcoded denylist (e.g., `metadata.google.internal`). The comments explicitly state: "We never perform a DNS lookup here".
  **Risk:** An attacker can bypass the hostname check by pointing a custom domain to `127.0.0.1` or `169.254.169.254` (DNS rebinding) since the MCP tool evaluates the name purely as text without resolving it.
  **Recommendation:** Ensure strict egress filtering and DNS resolution checking are applied at the network or gateway layer on the server before dialing out, to fully close the SSRF pivot vector.

### 4. Performance & Scaling

**Findings:**
- **Subscription Isolation:** The design cleanly splits event reading into two distinct tools: `session_watch` (fast, ≤ 30s timeout, max 50 events) and `session_subscribe` (agent-driven loops, up to 120s, up to 500 events). This ensures that short-lived standard UI requests do not queue behind long-running LLM subscriptions, thereby preserving overall server concurrency and responsiveness.

## Part 6: Platform Targets & Agent Swarm Management

This section of the code review focuses on the Platform code located in `packages/provide-uterm-platform/`, specifically analyzing local PTY captures, PAM authentication boundaries, LD_PRELOAD interceptors, and `uterm-manager` swarm orchestration. We evaluate the codebase against four critical lenses: Architecture & Health, Maintainability & Structure, Security & Concurrency, and Performance & Scaling.

### 1. Architecture & General Health (Data Flow, Composition)

**Findings:**
- **Swarm Orchestration:** `AgentManager` (`core.py`) successfully decouples swarm state orchestration from specific process management. It leverages a clean plugin architecture (`AccountPoolPlugin`, `TimeseriesPlugin`) and maintains clear state boundaries (`spawn_agent`, `spawn_swarm`, `kill_agent`).
- **Interception Mechanism:** `capture.c` elegantly leverages `DYLD_INTERPOSE` on macOS and `LD_PRELOAD` on Linux to intercept `read`, `write`, and `connect` syscalls. On macOS, it avoids two-level namespace resolution issues by resolving link-time addresses from the `__DATA,__interpose` struct's `.original` field, bypassing `dlsym` complexities entirely.

### 2. Maintainability & Structural Design

**Findings:**
- **Memory Safety in C:** The PAM module (`pam_uterm.c`) safely avoids buffer truncation risks by using dynamic allocation via `_build_json` (two-pass `vsnprintf` to calculate length, then allocate). It also safely uses `pam_get_user` over `pam_get_item(PAM_USER)` to sidestep known deadlocks in `libpam` implementations like Debian Linux-PAM.
- **Process Supervision:** `CaptureConnector` (`capture_connector.py`) implements a daemonless bridge to the intercepted shell. Instead of forking processes, it listens on a Unix socket for `libuterm_capture.so` to connect, greatly simplifying lifecycle management and keeping the platform agnostic to the spawned process tree.

### 3. Security & Concurrency Robustness

**Findings:**
- **Capture Socket Race Condition:** In `pam_listener.py`, `PamNotifyListener` uses `asyncio.start_unix_server` to bind the socket before calling `os.chmod(self._path, 0o600)`.
  **Risk:** This creates a race condition. The socket is created with the system's default umask before `os.chmod` restricts it. In this narrow window, an unauthorized local user could connect to the notification socket.
  **Recommendation:** Use `os.umask(0o177)` before binding the socket and restore the previous umask afterward to ensure atomic, safe permission boundaries upon creation.
- **Concurrent Send Interleaving:** In `capture.c`, `send_frame` issues two separate `write()` / `send()` syscalls: one for the 5-byte header (`[1B channel][4B length]`) and one for the payload. The `g_capture_fd` is shared globally without locks.
  **Risk:** If multiple threads in the captured process call `write()` concurrently, the OS might interleave the 5-byte header of one thread with the payload of another thread across the stream socket, completely corrupting the framing protocol.
  **Recommendation:** Wrap the dual `write()` calls in `send_frame` with a `pthread_mutex_t` to serialize frames, or better, use `writev()` / `sendmsg()` scatter-gather I/O to send the header and payload atomically in a single syscall.

### 4. Performance & Scaling

**Findings:**
- **Capture Buffer Memory Churn:** `CaptureConnector` (`capture_connector.py`) accumulates frames using string concatenation (`self._buffer += text`) and bounded slicing (`self._buffer = self._buffer[-65536:]`) on every incoming `poll_messages` chunk.
  **Risk:** In high-throughput terminal applications (e.g., `cat` on a large file or `htop`), executing these operations creates immense garbage collection pressure, as it copies strings continuously on every frame up to 64KB. This creates O(N^2) copying behavior under load.
  **Recommendation:** Replace the pure string buffer with a `collections.deque(maxlen=65536)` of string fragments (joining only upon snapshot request) or a pre-allocated circular bytearray to eliminate per-frame allocations.
- **Swarm Process Bottlenecks:** `AgentManager` maintains its swarm using localized `subprocess.Popen` handles. While effective for single-node scaling, this architecture is strictly bounded by the host OS process and file descriptor limits.
  **Recommendation:** For horizontal scaling beyond a single node, introduce a distributed job scheduler abstraction (e.g., Celery, Kubernetes Jobs) instead of purely local subprocesses.
