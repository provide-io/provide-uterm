# Feature Roadmap

Prioritized by novelty and competitive differentiation.

## Tier 1 — Novel (no competitor does this well for terminals)

### 1. Command Approval Workflows (Foundation Complete)
Pre-execution input gate that holds dangerous commands (`rm -rf /`, `DROP TABLE`)
pending human approval via Slack/webhook/REST. 
*   **Status:** Node-side `PolicyGate` and `WebhookPolicyGate` implemented. 
*   **Next:** State machine for "Hold & Resume" of buffered input.

### 2. Session Replay with AI Annotation
... [unchanged] ...

### 3. Multi-Session Fan-Out (Registry Complete)
Broadcast input to N sessions simultaneously.
*   **Status:** Node Discovery (registry heartbeats) implemented for fleet-wide bot/session tracking.

## Tier 2 — Useful, less differentiating

### 4. Real-Time Anomaly Detection
Pattern-matching on terminal output for credential leaks, privilege escalation,
destructive commands. Rule engine with regex, rolling-window, and LLM-based
detection. ARD: `docs/ard-realtime-anomaly-detection.md`.

### 5. Security Headers Middleware
CSP, HSTS, X-Frame-Options with configurable strict/dev modes. Quick win for
production security posture. Spec: `docs/superpowers/specs/2026-03-30-security-headers-design.md`.

### 6. Shell Render
Image-to-ANSI-art converter (static + animated GIF/APNG). Terminal-native
visualization. Spec: `docs/superpowers/specs/2026-03-29-shell-render-design.md`.

## Already Implemented

- **DeckMux Collaborative Presence** — ARD: `docs/ard-presence-collaboration-layer.md`
- **HTTP Interception (Phase 4)** — Spec: `docs/superpowers/specs/2026-04-01-http-intercept-modify-design.md`
- **Session Audit Recording** (partial) — ARD: `docs/ard-session-audit-compliance-recording.md`

## Architecture Decisions

- **FastAPI backend durability posture** — `docs/ard-fastapi-durability-posture.md`
- **Backend authz conformance testing** — `docs/ard-cross-backend-conformance-testing.md`
