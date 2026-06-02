# provide-uterm Code Review Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Conduct a comprehensive code review and architectural analysis of the Core Bridge System and Server Transports & Gateways, producing a detailed Markdown report.

**Architecture:** We will systematically review the relevant directories, taking notes on the four designated lenses (Health, Maintainability, Security, Performance), and then assemble the final report artifact.

**Tech Stack:** Python, FastAPI, asyncio, WebSockets.

---

### Task 1: Research & Write Core Bridge System (`TermHub`)

**Files:**
- Create: `artifacts/2026-06-02-code-review-report.md`

- [ ] **Step 1: Read and analyze Hub Services code**
  Read the core files in `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/` (like `core.py`, `registry.py`, `lease.py`, `limiter.py`, `router.py`) and `packages/provide-uterm/src/provide/uterm/bridge/worker_link.py`.
  Evaluate against the four lenses.

- [ ] **Step 2: Write Part 1 of the Report**
  Draft the findings for Part 1 (Core Bridge System) and save it to the report artifact file.

### Task 2: Research & Write Server Transports & Protocol Gateways

**Files:**
- Modify: `artifacts/2026-06-02-code-review-report.md`

- [ ] **Step 1: Read and analyze Gateway code**
  Read files in `packages/provide-uterm-server/src/provide/uterm/server/gateway/` (e.g., `telnet.py`, `ssh.py`, `websocket.py`).
  Evaluate abstraction boundaries, pluggability, security handshakes, and backpressure.

- [ ] **Step 2: Write Part 2 of the Report**
  Append the findings for Part 2 (Server Transports & Gateways) to the report artifact file.

### Task 3: Finalize and Polish Report

**Files:**
- Modify: `artifacts/2026-06-02-code-review-report.md`

- [ ] **Step 1: Review and refine the report**
  Ensure the report is extremely detailed, properly formatted, and directly addresses the four analysis lenses for both subsystems. Make any final polish edits.

- [ ] **Step 2: Commit the report**
  ```bash
  git add artifacts/2026-06-02-code-review-report.md
  git commit -m "docs: add comprehensive code review report"
  ```
