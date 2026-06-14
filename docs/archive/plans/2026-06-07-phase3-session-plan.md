# Phase 3 `<uterm-session>` Implementation Plan

> ✅ **COMPLETE (2026-06-08)** — landed on `main`, CI-green (full Playwright e2e 80 passed). The unchecked `- [ ]` boxes below are historical; the work is done. See `docs/superpowers/specs/2026-06-07-web-components-exploration.md`.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a composite `<uterm-session>` Lit Element that folds `hijack_impl.ts` (the bridge) together with `<uterm-terminal>`, transport logic, and all the Phase-1 leaf chrome into a single encapsulated structure. Maintain `ProvideHijack` as a backward-compatible delegator class.

**Architecture:** Instead of manually constructing the `DeckMux` wrappers and injecting them into a dynamically generated DOM tree, the entire UI tree will be declared inside `<uterm-session>`'s `render()` method (or instantiated as children). The state management and WebSocket orchestration currently residing in `ProvideHijack` will move inside this Lit Element. `ProvideHijack` itself will become a hollow shell that mounts `<uterm-session>` and proxies legacy API calls, preserving `window.ProvideHijack` for external consumers.

**Tech Stack:** TypeScript, Lit

---

## Task 1: Create `<uterm-session>` Lit Element

**Files:**
- Create: `packages/provide-uterm-frontend/src/session-element.ts`
- Create: `packages/provide-uterm-frontend/src/session-element.test.ts`

- [ ] **Step 1: Write Lit Component Skeleton**
Create `session-element.ts` with `@customElement('uterm-session')`.
Declare the reactive properties for the session state: config, connection state, users, etc.
Render the overall session UI layout (using the previously created Lit components like `<uterm-terminal>`, `<uterm-presence-bar>`, `<uterm-approval-prompt>`, etc.).

- [ ] **Step 2: Port `ProvideHijack` logic**
Move the WebSocket, state updates, and command execution logic from `hijack_impl.ts` into the Lit Element.
Wire the Lit Element to update its internal properties and pass them down to its children components natively.

- [ ] **Step 3: Component Tests**
Write basic mounting and state tests in `session-element.test.ts`.

- [ ] **Step 4: Commit**
`git commit -m "feat(frontend): create <uterm-session> composite element"`

---

## Task 2: Refactor `ProvideHijack` to Delegate

**Files:**
- Modify: `packages/provide-uterm-frontend/src/hijack_impl.ts`

- [ ] **Step 1: Swap internal tree for Lit Element**
Refactor the constructor of `ProvideHijack` to instantiate and append `<uterm-session>`.

- [ ] **Step 2: Proxy API calls**
Delegate `ProvideHijack`'s public API (e.g., `.dispose()`, `.configure()`) to the underlying `<uterm-session>`.

- [ ] **Step 3: Fix Broken Tests**
Run `npm run test`. Fix the comprehensive `hijack.test_part1.ts` and `hijack-extra.test_part1.ts` files, which previously relied on deep DOM assertions and `_approvalTimer` internals. Update them to target the Shadow DOM (or light DOM) of the `<uterm-session>`.

- [ ] **Step 4: Commit**
`git commit -m "refactor(frontend): ProvideHijack delegates to <uterm-session>"`

---

## Task 3: Cleanup and Verification

**Files:**
- Modify: `packages/provide-uterm-frontend/static/hijack.css` (if needed)

- [ ] **Step 1: CSS Migration**
Migrate any overarching `hijack.css` styles into the `static styles` of `<uterm-session>`.

- [ ] **Step 2: Final Verification**
Ensure all ~502 tests pass successfully with `npm run test`.

- [ ] **Step 3: Commit**
`git commit -m "chore(frontend): finalize phase 3 session migration"`
