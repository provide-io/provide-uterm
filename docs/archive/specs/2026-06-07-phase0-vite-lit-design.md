# Phase 0: Vite + Lit Migration Design

## Purpose
This document specifies the design for Phase 0 of migrating `provide-uterm-frontend` from a `tsc`-only build to Web Components (Lit) and Vite. This establishes the foundation for healing the build-split between the two frontends and validates the new toolchain without disrupting existing features.

## 1. Frontend Build Pipeline (Vite + Lit)

`provide-uterm-frontend` will be updated to use Vite to produce hashed bundles and a manifest, replacing the current 1:1 `tsc` emission.

### Dependencies & Tooling
- Add `lit` (v3.x) and `vite` (v6.x or latest) to `packages/provide-uterm-frontend/package.json`.
- Update `tsconfig.json` to enable Lit decorators:
  ```json
  {
    "compilerOptions": {
      "experimentalDecorators": true,
      "useDefineForClassFields": false
    }
  }
  ```

### Vite Configuration (`vite.config.ts`)
- Use Rollup's multi-input configuration for `src/hijack.ts` and `src/terminal.ts`.
- Output directory: `../../packages/provide-uterm-server/src/provide/uterm/server/frontend/` (matching existing `tsc` outDir).
- Enable manifest generation with a distinct filename: `build.manifest = "vanilla-manifest.json"`.

## 2. Python Server Integration (`ui.py`)

The server must consume the new hashed Vite bundles instead of hardcoding `hijack.js` and relying on the `mtime` query parameter.

### Manifest Parsing
- Update `_read_vite_manifest()` to parse both `manifest.json` (React) and the new `vanilla-manifest.json` (Vanilla).
- Cache both manifests in memory on first load.

### Dynamic Resolution
- Create `_resolve_vanilla_asset(entry_name: str) -> str` to look up entries (like `src/hijack.ts`) in the vanilla manifest and return the hashed output filename.
- Update `session_page_html()`: Replace `f"hijack.js?v={_hijack_js_version()}"` with the dynamically resolved asset.
- Remove the obsolete `_hijack_js_version()` function.

## 3. The Component Proof (`<uterm-toast-stack>`)

To prove the Lit toolchain and its testability, we will create a new throwaway component.

### Implementation
- Create `src/app/deckmux/toast-stack.ts`.
- Define `@customElement('uterm-toast-stack')` using Lit.
- Encapsulate styling using `static styles = css...` and Shadow DOM.

### Testing
- Create `src/app/deckmux/toast-stack.test.ts`.
- Validate the new testing paradigm: instantiate the element, `document.body.appendChild(el)`, and `await el.updateComplete` before making assertions on `el.shadowRoot`.

## Conclusion
Completion of Phase 0 proves the Vite + Lit toolchain, enabling subsequent phases to confidently port complex components (`<uterm-terminal>`, `<uterm-session>`) while enjoying hashed cache-busting out of the box.
