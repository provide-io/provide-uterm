# Phase 0 Vite+Lit Migration Implementation Plan

> ✅ **COMPLETE (2026-06-08)** — landed on `main`, CI-green (full Playwright e2e 80 passed). The unchecked `- [ ]` boxes below are historical; the work is done. See `docs/superpowers/specs/2026-06-07-web-components-exploration.md`.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `provide-uterm-frontend` from `tsc` to Vite+Lit, and update the Python server to consume the new hashed Vite manifest.

**Architecture:** Vite builds the vanilla frontend emitting `vanilla-manifest.json`. `ui.py` reads this manifest to inject hashed entry points (e.g., `hijack.js`) into server-rendered HTML. A new Lit component `<uterm-toast-stack>` proves the toolchain.

**Tech Stack:** TypeScript, Vite, Lit, Python (FastAPI).

---

## Task 1: Frontend Build Config (Vite + TS)

**Files:**
- Modify: `packages/provide-uterm-frontend/package.json`
- Modify: `packages/provide-uterm-frontend/tsconfig.json`
- Create: `packages/provide-uterm-frontend/vite.config.ts`

- [ ] **Step 1: Add dependencies to package.json**

Modify `packages/provide-uterm-frontend/package.json`:
Change the `build` script to `"vite build"`.
Add `lit` to dependencies (or devDependencies).

```json
  "scripts": {
    "build": "vite build",
    ...
  },
  "dependencies": {
    "lit": "^3.1.2"
  },
```

- [ ] **Step 2: Update tsconfig.json for Lit**

Modify `packages/provide-uterm-frontend/tsconfig.json`:

```json
    "experimentalDecorators": true,
    "useDefineForClassFields": false,
```
(Add these inside `compilerOptions`).

- [ ] **Step 3: Create vite.config.ts**

Create `packages/provide-uterm-frontend/vite.config.ts`:

```typescript
import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  build: {
    outDir: '../../packages/provide-uterm-server/src/provide/uterm/server/frontend',
    emptyOutDir: false,
    manifest: 'vanilla-manifest.json',
    rollupOptions: {
      input: {
        hijack: resolve(__dirname, 'src/hijack.ts'),
        terminal: resolve(__dirname, 'src/terminal.ts')
      }
    }
  }
});
```

- [ ] **Step 4: Run Vite build**

Run: `cd packages/provide-uterm-frontend && npm i && npm run build`
Expected: Outputs to the server's `frontend/` directory with `vanilla-manifest.json`.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-frontend/package.json packages/provide-uterm-frontend/tsconfig.json packages/provide-uterm-frontend/vite.config.ts
git commit -m "build(frontend): migrate to vite and lit toolchain"
```

---

## Task 2: Python Server Integration

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/ui.py`
- Test: `packages/provide-uterm-server/tests/test_ui.py` (if exists, or verify manually)

- [ ] **Step 1: Update manifest reading in ui.py**

Modify `ui.py` to add `_vanilla_manifest` caching, and a new `_read_vanilla_manifest()` function exactly like `_read_vite_manifest()` but pointing to `vanilla-manifest.json`.

```python
_vanilla_manifest: dict[str, object] | None = None
_vanilla_manifest_loaded = False

def _read_vanilla_manifest() -> dict[str, object] | None:
    global _vanilla_manifest, _vanilla_manifest_loaded
    if _vanilla_manifest_loaded:
        return _vanilla_manifest
    _vanilla_manifest_loaded = True
    try:
        manifest_path = importlib.resources.files("provide.uterm.server") / "frontend" / ".vite" / "vanilla-manifest.json"
        if not manifest_path.is_file():
            # Fallback to root dir if vite doesn't use .vite
            manifest_path = importlib.resources.files("provide.uterm.server") / "frontend" / "vanilla-manifest.json"
        if manifest_path.is_file():
            raw = manifest_path.read_text(encoding="utf-8")
            _vanilla_manifest = json.loads(raw)
            logger.info("vanilla_manifest loaded entries=%d", len(_vanilla_manifest or {}))
    except Exception:
        pass
    return _vanilla_manifest

def _resolve_vanilla_asset(entry_name: str) -> str:
    manifest = _read_vanilla_manifest()
    if manifest and entry_name in manifest:
        entry = manifest[entry_name]
        if isinstance(entry, dict) and "file" in entry:
            return str(entry["file"])
    # fallback
    return entry_name.split("/")[-1].replace(".ts", ".js")
```

- [ ] **Step 2: Update session_page_html**

Remove `_hijack_js_version()`. Update `session_page_html()`:

```python
        pre_vite_modules=(f"{_resolve_vanilla_asset('src/hijack.ts')}",),
```

- [ ] **Step 3: Test Python Syntax**

Run: `uv run ruff check packages/provide-uterm-server/src/provide/uterm/server/ui.py`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/ui.py
git commit -m "feat(server): serve frontend assets via vite manifest"
```

---

## Task 3: `<uterm-toast-stack>` Component Proof

**Files:**
- Create: `packages/provide-uterm-frontend/src/app/deckmux/toast-stack.ts`
- Create: `packages/provide-uterm-frontend/src/app/deckmux/toast-stack.test.ts`

- [ ] **Step 1: Write Lit Component**

Create `packages/provide-uterm-frontend/src/app/deckmux/toast-stack.ts`:

```typescript
import { LitElement, html, css } from 'lit';
import { customElement } from 'lit/decorators.js';

@customElement('uterm-toast-stack')
export class ToastStack extends LitElement {
  static override styles = css`
    :host {
      display: block;
      position: fixed;
      bottom: 20px;
      right: 20px;
    }
    .toast {
      background: var(--dm-bg, #333);
      color: white;
      padding: 12px;
      border-radius: 4px;
      margin-top: 8px;
    }
  `;

  override render() {
    return html`
      <div class="toast-container">
        <slot></slot>
      </div>
    `;
  }
}
```

- [ ] **Step 2: Write Test**

Create `packages/provide-uterm-frontend/src/app/deckmux/toast-stack.test.ts`:

```typescript
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import './toast-stack.js';

describe('uterm-toast-stack', () => {
  let el: HTMLElement;

  beforeEach(async () => {
    el = document.createElement('uterm-toast-stack');
    document.body.appendChild(el);
    // @ts-ignore
    await el.updateComplete;
  });

  afterEach(() => {
    el.remove();
  });

  it('renders a toast container', () => {
    expect(el.shadowRoot).toBeTruthy();
    expect(el.shadowRoot!.querySelector('.toast-container')).toBeTruthy();
  });
});
```

- [ ] **Step 3: Run Tests**

Run: `cd packages/provide-uterm-frontend && npm run test`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add packages/provide-uterm-frontend/src/app/deckmux/toast-stack.ts packages/provide-uterm-frontend/src/app/deckmux/toast-stack.test.ts
git commit -m "feat(frontend): introduce lit-based toast stack component"
```
