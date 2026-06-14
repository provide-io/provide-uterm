# Feature Roadmap

Prioritized by novelty and competitive differentiation.

## Tier 1 — Novel (no competitor does this well for terminals)

### 1. Command Approval Workflows (AGPL-3.0-or-later)
Pre-execution input gate that holds dangerous commands (`rm -rf /`, `DROP TABLE`)
pending human approval via Slack/webhook/REST.
*   **Status:** **Buffered Hold & Resume Done** — dangerous commands can be held for approval, subsequent keystrokes are buffered while the browser is paused, and buffered input is replayed after approval resolution.
*   **Next:** Harden full-package verification and expand approval workflow coverage across broader server/API paths.

### 2. Session Replay with AI Annotation (AGPL-3.0-or-later)
Automatic summarization of terminal sessions using LLMs. Generates searchable "Chapters" and "Key Actions" for long audit logs.
*   **Status:** **Pattern-Based Annotation Done (2026-04-08)** — JSONL recording, replay viewer, raw stream rebuilder, `PatternDetector` with 20 built-in detection rules (credentials, escalation, destructive commands, connections, lifecycle), `Annotation`/`AnnotationSpan` data models, REST endpoint (`POST /api/sessions/{id}/annotate`), and `session_annotate` MCP tool (tool 21 of 21) all implemented and tested.
*   **Parked:** LLM-based summarization pipeline (auto-generated "Chapters" and "Key Actions") has no implementation or active development. The annotation system uses regex pattern detection only — no LLM integration exists in the annotation or recording modules.

### 3. Multi-Session Fan-Out (AGPL-3.0-or-later)
Managed fleet control UI. Broadcast input to N sessions simultaneously with group management and status aggregation.
*   **Status:** **Server-Side Complete (2026-04-22)** — `FanOutController` with parallel/sequential broadcast, Levenshtein-based divergence detection, `FanOutStore` protocol + in-memory implementation, REST routes (CRUD groups, send, grants), `FanOutPolicyGate` with webhook support, distributed command approval integration, RBAC authorization, audit events, `fanout_group_create` and `fanout_send` MCP tools, E2E tests (13 Docker SSH + 15 full-stack scenarios). Enterprise hardening pass completed 2026-04-22 (memory leak fixes, approval expiration pruning).
*   **Parked:** "Fleet Console" browser UI has no implementation. All fan-out interaction is currently REST API and MCP tooling only.

## Tier 2 — Infrastructure & Security (All AGPL)

### 4. Real-Time Anomaly Detection
Pattern-matching on terminal output for credential leaks, privilege escalation, and destructive commands.
*   **Status:** **Done (2026-04-04)** — `PromptDetector` (cursor-aware prompt region scanning with two-pass detection), `DetectionEngine` (rule-based prompt detection + KV extraction with buffering and idle detection), `PatternDetector` (annotation-layer hot-path scanner with per-category dedup), `BehavioralAuditGate` protocol (CPS/jitter anomaly detection with webhook delegation). 20 built-in detection rules across 5 categories.

### 5. Security Headers Middleware
CSP, HSTS, X-Frame-Options with configurable strict/dev modes.
*   **Status:** **Done (2026-04-04)** — `SecurityHeadersMiddleware` implemented and wired into FastAPI.

### 6. Shell Render
Image-to-ANSI-art converter (static + animated GIF/APNG). Terminal-native visualization.
*   **Status:** **Done (2026-04-04)** — `render` module with `image_to_ansi_frames`, `render_frame` (half-block pixel rendering), three color modes (truecolor/256/16), palette quantizers (`nearest_16`, `nearest_256`), SGR escape emitters, and `shell/_render.py` integration layer. Absorbed into core package 2026-04-04.

## Already Implemented

- **DeckMux Collaborative Presence** — Current protocol: `docs/protocol-matrix.md`
- **HTTP Interception (Phase 4)**
- **Session Audit Recording** (Core) — ARD: `docs/ard-session-audit-compliance-recording.md`
- **Shared REST Contracts** — Drastically reduced drift between Server and Cloudflare backends.
- **Protocol Versioning** — Explicit handshake negotiation between nodes.

## Architecture Decisions

- **FastAPI backend durability posture** — `docs/ard-fastapi-durability-posture.md`
- **Backend authz conformance testing** — `docs/ard-cross-backend-conformance-testing.md`
