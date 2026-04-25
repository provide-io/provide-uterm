# Feature Roadmap

Prioritized by novelty and competitive differentiation.

## Tier 1 — Novel (no competitor does this well for terminals)

### 1. Command Approval Workflows (AGPL-3.0-or-later)
Pre-execution input gate that holds dangerous commands (`rm -rf /`, `DROP TABLE`)
pending human approval via Slack/webhook/REST.
*   **Status:** **Foundation Done** — Node-side `PolicyGate`, `WebhookPolicyGate`, and single-command "Hold & Resume" implemented.
*   **Next:** State machine for "Hold & Resume" of buffered input (buffering keys sent *while* a command is pending).

### 2. Session Replay with AI Annotation (Proprietary / Enterprise)
Automatic summarization of terminal sessions using LLMs. Generates searchable "Chapters" and "Key Actions" for long audit logs.
*   **Status:** **Recording Done** — JSONL/Asciinema recording implemented.
*   **Next:** AI pipeline for post-processing recordings into human-readable summaries.

### 3. Multi-Session Fan-Out (Proprietary / Enterprise)
Managed fleet control UI. Broadcast input to N sessions simultaneously with group management and status aggregation.
*   **Status:** **Core Done** — Node Discovery (registry heartbeats) and `FanOutController` (broadcast logic) implemented.
*   **Next:** Commercial "Fleet Console" UI for managing thousands of sessions.

## Tier 2 — Infrastructure & Security (All AGPL)

### 4. Real-Time Anomaly Detection
Pattern-matching on terminal output for credential leaks, privilege escalation, and destructive commands.
*   **Status:** **Done** — `TerminalDetector` with regex, rolling-window, and async LLM engine implemented.

### 5. Security Headers Middleware
CSP, HSTS, X-Frame-Options with configurable strict/dev modes.
*   **Status:** **Done** — `SecurityHeadersMiddleware` implemented and wired into FastAPI.

### 6. Shell Render
Image-to-ANSI-art converter (static + animated GIF/APNG). Terminal-native visualization.
*   **Status:** **Done** — `ShellRender` and `AnsiArt` primitives implemented.

## Already Implemented

- **DeckMux Collaborative Presence** — ARD: `docs/ard-presence-collaboration-layer.md`
- **HTTP Interception (Phase 4)** — Spec: `docs/superpowers/specs/2026-04-01-http-intercept-modify-design.md`
- **Session Audit Recording** (Core) — ARD: `docs/ard-session-audit-compliance-recording.md`
- **Shared REST Contracts** — Drastically reduced drift between Server and Cloudflare backends.
- **Protocol Versioning** — Explicit handshake negotiation between nodes.

## Architecture Decisions

- **FastAPI backend durability posture** — `docs/ard-fastapi-durability-posture.md`
- **Backend authz conformance testing** — `docs/ard-cross-backend-conformance-testing.md`
