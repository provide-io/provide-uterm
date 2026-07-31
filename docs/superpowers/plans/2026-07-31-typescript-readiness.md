# TypeScript Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore TypeScript package build and coverage gates and make runtime maturity claims match integrated server behavior.

**Architecture:** Keep the package's ES2023 target in both typecheck and emit configurations, make emit a mandatory CI gate, and use capability-driven documentation rather than describing isolated modules as served functionality.

**Tech Stack:** TypeScript 7, Vitest/V8 coverage, Biome, GitHub Actions.

---

### Task 1: Restore the emit build

**Files:**
- Modify: `packages/provide-uterm-ts/tsconfig.build.json`
- Test: `packages/provide-uterm-ts/package.json`

- [ ] **Step 1: Reproduce the red build**

```bash
npm run build --workspace=packages/provide-uterm-ts
```

Expected: TS2550/TS2554 errors because the build config replaces inherited ES2023 libs with DOM only.

- [ ] **Step 2: Retain ES2023 during emit**

Set the build `lib` array to:

```json
"lib": ["ES2023", "DOM"]
```

- [ ] **Step 3: Verify emitted artifacts and commit**

```bash
npm run build --workspace=packages/provide-uterm-ts
test -f packages/provide-uterm-ts/dist/index.js
git add packages/provide-uterm-ts/tsconfig.build.json
git commit -m "fix(ts): restore ES2023 emit build"
```

### Task 2: Restore strict coverage

**Files:**
- Modify: `packages/provide-uterm-ts/src/serverauth/serverauth.test.ts`
- Modify only if another reported branch is semantic: `packages/provide-uterm-ts/src/egress/egress.test.ts`

- [ ] **Step 1: Preserve the failing coverage evidence**

```bash
npm run test:ts:coverage
```

Expected: tests pass but the 100% threshold fails, including `principal.ts:50`.

- [ ] **Step 2: Add the normalization boundary test**

Call `applyCfAccessTeamDomain` with `https://.cloudflareaccess.com/` and assert it does not synthesize JWKS or issuer values. Add only the precise semantic egress case reported by V8 if still uncovered.

- [ ] **Step 3: Run coverage and commit**

```bash
npm run test:ts:coverage
git add packages/provide-uterm-ts/src
git commit -m "test(ts): restore strict coverage gate"
```

### Task 3: Make build mandatory and maturity claims accurate

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `packages/provide-uterm-ts/README.md`
- Modify: `docs/typescript-port-roadmap.md`
- Modify: `docs/protocol-matrix.md`
- Modify: `docs/roadmap/uterm-code-review-remediation.md`

- [ ] **Step 1: Add the CI build command**

Insert `npm run build --workspace=packages/provide-uterm-ts` immediately after `typecheck:ts`. Keep coverage as a separate later gate.

- [ ] **Step 2: Replace “full port” claims**

Describe the package as a high-coverage partial runtime port. List the four currently served HTTP capabilities and explicitly separate completed libraries from integrated server surfaces. State that TypeScript joins Playwright parity after browser/worker WebSockets and lifecycle routes are served.

- [ ] **Step 3: Verify all TypeScript gates**

```bash
npm run typecheck:ts
npm run lint:ts
npm run build --workspace=packages/provide-uterm-ts
npm run test:ts:coverage
```

- [ ] **Step 4: Update tracker and commit**

Record results and complete `TS-BUILD-001`, `TS-CI-001`, `TS-COV-001`, `TS-DOC-001`, and `TS-E2E-001`.

