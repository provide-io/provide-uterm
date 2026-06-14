# Phase 2 `<uterm-terminal>` Implementation Plan

> ✅ **COMPLETE (2026-06-08)** — landed on `main`, CI-green (full Playwright e2e 80 passed). The unchecked `- [ ]` boxes below are historical; the work is done. See `docs/superpowers/specs/2026-06-07-web-components-exploration.md`.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap the `xterm` library initialization inside a reactive, light-DOM Lit component (`<uterm-terminal>`), while leaving `ProvideTerminal` as a backward-compatible delegator class to ensure no downstream breakage.

**Architecture:** Because `xterm` needs to compute styles and interact heavily with the DOM (IME, focus, copy/paste), putting it in a Shadow DOM causes severe issues. Thus, `<uterm-terminal>` will override `createRenderRoot()` to return `this` (light DOM). The existing `ProvideTerminal` class will instantiate this web component instead of its imperative DOM hierarchy, proxying all config, events, and lifecycle methods to it.

**Tech Stack:** TypeScript, Lit, xterm.js

---

## Task 1: Create `<uterm-terminal>` Lit Element

**Files:**
- Create: `packages/provide-uterm-frontend/src/terminal-element.ts`
- Create: `packages/provide-uterm-frontend/src/terminal-element.test.ts`

- [ ] **Step 1: Write Lit Component Skeleton**
Create `terminal-element.ts` containing the skeleton for `@customElement('uterm-terminal')`.
Include `createRenderRoot() { return this; }` to enforce light DOM rendering.
Include `static styles` (or standard class additions) where possible, but remember that light DOM elements don't get true scoping.

- [ ] **Step 2: Port xterm initialization**
Port the xterm and FitAddon instantiation logic from `ProvideTerminal` into `terminal-element.ts` (inside `firstUpdated` or `connectedCallback`).
Add reactive properties for `config` (TerminalConfig).

- [ ] **Step 3: Write Component Tests**
Create `terminal-element.test.ts`. Test that the element attaches to the DOM and that xterm initializes correctly. (Use side-effect imports).

- [ ] **Step 4: Run Tests**
Run: `npm run test`
Expected: PASS

- [ ] **Step 5: Commit**
`git commit -m "feat(frontend): create <uterm-terminal> light-DOM element"`

---

## Task 2: Refactor `ProvideTerminal` to Delegate

**Files:**
- Modify: `packages/provide-uterm-frontend/src/terminal_impl.ts`

- [ ] **Step 1: Swap DOM tree for Lit Element**
Update `ProvideTerminal`'s constructor. Remove the manual `createElement` logic for the terminal wrapper and instead instantiate `document.createElement('uterm-terminal')`.

- [ ] **Step 2: Wire up proxies**
Proxy `_term.write()`, `_term.onData()`, and WebSocket logic to interact with the new Lit element. If the Lit element owns the WebSocket, ensure the `ProvideTerminal` public API (like `.dispose()`, `.focus()`) still operates identically by delegating to the Lit element methods.

- [ ] **Step 3: Run Tests**
Run `npm run test` and fix any `ProvideTerminal` tests in `terminal.test_part1.ts` that break due to DOM changes.

- [ ] **Step 4: Commit**
`git commit -m "refactor(frontend): ProvideTerminal delegates to <uterm-terminal>"`

---

## Task 3: Cleanup and Verification

**Files:**
- Modify: `packages/provide-uterm-frontend/static/terminal.css` (if needed)

- [ ] **Step 1: CSS Migration**
Verify if any CSS rules from `terminal.css` need to be updated to target `uterm-terminal`.

- [ ] **Step 2: Full Test Suite Verification**
Run `npm run test` to verify all ~499 tests pass.

- [ ] **Step 3: Commit**
`git commit -m "chore(frontend): finalize phase 2 terminal migration"`
