# Lane A7 — Frontend Hardening Plan (small)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first.

**Goal:** Fix the one frontend robustness finding. (XSS/terminal-rendering surfaces were audited in the review and found correctly escaped — `term.write()` for terminal bytes, `textContent`/`escapeHijackHtml` everywhere user data reaches the DOM. No XSS work is required.)

**Scope (exclusive write ownership):** `packages/provide-uterm-frontend/**` only. This is a TS/vitest/biome toolchain, fully isolated from the Python lanes — it can run alongside everything in Wave A. `src/generated/frames.ts` is auto-generated; do NOT edit it.

**Tech Stack:** Vanilla TypeScript, xterm.js, vitest, biome.

---

## Tasks

### Task 1 (FE-sel 🟢): Harden the DeckMux `querySelector` userId interpolation

**Files:**
- Modify: `packages/provide-uterm-frontend/src/app/deckmux/deckmux.ts:~415`
- Test: the deckmux test file (vitest)

**Problem:** `this._barContainer?.querySelector('[data-user-id="${userId}"]')` interpolates a server-supplied `userId` into a CSS selector. Not an XSS sink, but a `userId` containing `"` or `]` throws a `SyntaxError` that breaks the avatar-click handler.

- [ ] **Step 1: Read** `deckmux.ts` around line 415 and how `this._users` (or an element map) is maintained.
- [ ] **Step 2: Write failing test:**

```ts
import { describe, it, expect } from "vitest";

it("avatar lookup survives a userId with CSS-special chars", () => {
  const dm = makeDeckMux();              // test harness factory
  dm.addUser({ userId: 'a"]b', name: "x" });
  expect(() => dm.handleAvatarClick('a"]b')).not.toThrow();
});
```

- [ ] **Step 3: Run, expect FAIL.** `npm test -w packages/provide-uterm-frontend -- -t "avatar lookup"` (FAIL with `SyntaxError`).
- [ ] **Step 4: Implement.** Prefer a map lookup over a selector (look the element up in `this._users`/an element registry keyed by `userId`). If a selector is genuinely needed, wrap with `CSS.escape`:

```ts
const el = this._barContainer?.querySelector(`[data-user-id="${CSS.escape(userId)}"]`);
```
(Map lookup is preferred — it avoids the selector entirely.)

- [ ] **Step 5: Run, expect PASS** + `npm run typecheck:frontend && npm run lint:frontend`.
- [ ] **Step 6: Commit** — `fix(frontend): harden deckmux avatar lookup against CSS-special userIds`

---

### Optional cleanup (only if time permits — separate commits)
- `hijack_impl.ts:209,219,249,275,757` — replace `(window as any)`/`(root as any)` casts for CDN globals with typed `Window` augmentations (mirror `terminal_impl.ts:74-81`). Pure type-safety improvement.
- `hijack_impl.ts` — remove the dead read of `msg.hijacked_by_me` on the `hello` frame (CF `hello` frames never send it).

---

### Done criteria (Lane A7)
- [ ] `npm test -w packages/provide-uterm-frontend` green
- [ ] `npm run typecheck:frontend && npm run lint:frontend` clean
- [ ] Commit(s) per logical unit.

### Cross-lane requests
_(none expected)_
