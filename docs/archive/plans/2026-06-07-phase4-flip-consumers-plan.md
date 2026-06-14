# Phase 4 `<uterm-session>` Flip Consumers Implementation Plan

> ✅ **COMPLETE (2026-06-08)** — landed on `main`, CI-green (full Playwright e2e 80 passed). The unchecked `- [ ]` boxes below are historical; the work is done. See `docs/superpowers/specs/2026-06-07-web-components-exploration.md`.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flip both the vanilla Python server consumer and the React `provide-ui` consumer over to natively using the new `<uterm-session>` and `<uterm-terminal>` Web Components. Then, delete the old `ProvideHijack` bridge layer entirely.

**Architecture:** Up until now, `ProvideHijack` and `ProvideTerminal` acted as proxies to preserve backward compatibility. Now that the Web Components are robust and tested, we eliminate the proxies. In `ui.py`, the generated HTML will use `<uterm-session id="app"></uterm-session>` directly. In React (`HijackHost.tsx`), it will render `<uterm-session>` natively. Once both consumers are migrated, we permanently delete `hijack_impl.ts`, `terminal_impl.ts`, and `hijack-widget-host.ts`.

**Tech Stack:** TypeScript, Lit, React, Python (FastAPI)

---

## Task 1: Migrate Vanilla Server (`ui.py`)

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/ui.py`
- Modify: `packages/provide-uterm-frontend/src/hijack.ts` (entry point)
- Modify: `packages/provide-uterm-frontend/src/terminal.ts` (entry point)

- [ ] **Step 1: Update Server HTML**
In `ui.py`, update `session_page_html()` to render `<uterm-session id="app"></uterm-session>` directly into the HTML instead of executing `window.ProvideHijack(...)` script blocks. Pass the initial config JSON safely into an attribute or a script tag that the element will read on boot. Do the same for `terminal_page_html()` rendering `<uterm-terminal>`.

- [ ] **Step 2: Update Entry Points**
Update `hijack.ts` to just import the Lit elements (and maybe auto-mount them if they rely on a bootloader script, or they can just be self-assembling Web Components on the page).

- [ ] **Step 3: Run Server Tests**
Ensure the server builds and passes any local tests.

- [ ] **Step 4: Commit**
`git commit -m "refactor(server): flip vanilla UI to use web components natively"`

---

## Task 2: Migrate React Consumer (`HijackHost.tsx`)

**Files:**
- Modify: `packages/provide-ui/src/hijack/HijackHost.tsx` (or whatever the path is for the React consumer).

- [ ] **Step 1: Swap React wrapper**
Update the React component to return `<uterm-session ...props></uterm-session>`. Use a React ref to imperatively call methods on it if needed (like `dispose()` or `focus()`), or rely on React 19's native support for setting complex properties on Web Components.

- [ ] **Step 2: Run React Tests**
Run the React suite if applicable.

- [ ] **Step 3: Commit**
`git commit -m "refactor(ui): flip React HijackHost to use <uterm-session>"`

---

## Task 3: Delete the Bridge (The Point of No Return)

**Files:**
- Delete: `packages/provide-uterm-frontend/src/hijack_impl.ts`
- Delete: `packages/provide-uterm-frontend/src/terminal_impl.ts`
- Delete: `packages/provide-uterm-frontend/src/app/widgets/hijack-widget-host.ts`
- Modify: Any files that referenced `ProvideHijack` internally

- [ ] **Step 1: Delete Bridge Files**
Delete the old imperative wrapper classes. The Web Components are now the sole source of truth.

- [ ] **Step 2: Refactor Test Suites**
You will need to heavily refactor the main test suites (`hijack.test_part1.ts`, `terminal.test_part1.ts`, etc.) to import `UtermSessionElement` and `UtermTerminalElement` directly instead of `ProvideHijack`. Replace the test instantiation logic with `document.createElement('uterm-session')`.

- [ ] **Step 3: Verify All Tests**
Run `npm run test` and ensure all 502+ tests pass with the pure Web Component architecture.

- [ ] **Step 4: Commit**
`git commit -m "refactor(frontend): delete legacy ProvideHijack bridge"`
