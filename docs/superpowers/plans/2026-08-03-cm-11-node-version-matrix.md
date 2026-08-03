# CM-11: Node 22/24/26 Matrix and Engine Agreement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, typecheck, lint and test on Node 22, 24 and 26; make the
declared engine ranges agree with what CI proves; and stop the app tests
depending on Node's experimental process-global `localStorage`.

**Architecture:** Fix the jsdom dependency first — it is the reason the tests
are version-sensitive at all — then reconcile the engine ranges, then add the
matrix. Adding the matrix first would just produce three red cells with one
cause.

**Tech Stack:** Node 22/24/26, npm workspaces, vitest, jsdom, GitHub Actions.

## Global Constraints

- The declared `engines` range and the CI matrix must agree. A package claiming
  `>=20` while CI proves only 22 is a claim nothing backs.
- TypeScript coverage stays at 100% on all four metrics.
- No test may depend on an experimental Node global. Browser storage is
  browser-environment behavior and belongs in jsdom.
- CI job count grows; keep the matrix to build/typecheck/lint/test rather than
  duplicating heavier jobs across three versions.

## Context

Measured 2026-08-03:

```
$ cat .nvmrc
22

$ grep -n '"node"' package.json packages/*/package.json
package.json:5:                     "node": ">=20",
packages/provide-uterm-ts/package.json:9:  "node": ">=22"

$ grep -n "node-version" .github/workflows/ci.yml
(8 occurrences, all node-version-file: ".nvmrc")
```

Every one of the eight CI setup steps pins `.nvmrc`, so only Node 22 is ever
exercised. The root package claims `>=20` and nothing has ever run on 20 or 21.
The TypeScript package claims `>=22`. The two disagree, and neither is tested at
its stated floor.

The app tests use `localStorage` directly —
`packages/provide-uterm-app/src/components/connect/ConnectPage.test.tsx:22,98,108`
and the component at `ConnectPage.tsx:26,40`. Node exposes a process-global
`localStorage` as an experimental feature whose availability and behavior differ
across 22, 24 and 26, which is what the quality-evidence design means by
"defines browser storage through jsdom rather than depending on Node's
experimental process-global."

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-11.

---

### Task 1: App tests get storage from jsdom

**Files:**
- Modify: `packages/provide-uterm-app/vitest.config.ts` (or the app's existing
  vitest config — locate in Step 1)
- Modify: `packages/provide-uterm-app/src/components/connect/ConnectPage.test.tsx`
- Possibly create: `packages/provide-uterm-app/src/test-setup.ts`

- [ ] **Step 1: Find the current test environment**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
cat packages/provide-uterm-app/vitest.config.ts 2>/dev/null || ls packages/provide-uterm-app/
grep -rn "environment" packages/provide-uterm-app/vitest.config.* 2>/dev/null
```

Record whether `environment: "jsdom"` is already set. If it is, the tests may be
picking up Node's global in preference — check which `localStorage` they
actually bind by adding a temporary assertion.

- [ ] **Step 2: Write the failing test**

Add to `ConnectPage.test.tsx`:

```typescript
it("uses the jsdom window storage, not a Node process global", () => {
  // Node exposes an experimental process-global localStorage whose behavior
  // differs across 22, 24 and 26. Binding to it makes these tests
  // version-sensitive for no benefit — the component under test is browser
  // code and should see the browser's storage.
  expect(globalThis.localStorage).toBe(window.localStorage);
});
```

- [ ] **Step 3: Run it**

Run:
```bash
cd packages/provide-uterm-app
npx vitest run src/components/connect/ConnectPage.test.tsx
```

If it passes, jsdom is already the binding and this task reduces to pinning that
with the assertion — record that and skip to Step 5. If it fails, continue.

- [ ] **Step 4: Configure jsdom explicitly**

In the app's vitest config, set:

```typescript
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
  },
```

and in `src/test-setup.ts`:

```typescript
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// jsdom supplies window.localStorage. Node also exposes a process-global
// localStorage as an experimental feature, and which one a bare `localStorage`
// reference resolves to varies by Node version. Bind it explicitly so the tests
// exercise browser storage on every version.
globalThis.localStorage = window.localStorage;
```

- [ ] **Step 5: Verify**

Run:
```bash
cd packages/provide-uterm-app
npx vitest run
npm run typecheck
```

Expected: PASS, all app tests.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-app/
git commit -m "test(app): bind browser storage to jsdom explicitly

Node exposes an experimental process-global localStorage whose
availability and behavior differ across 22, 24 and 26, and a bare
localStorage reference could resolve to either it or jsdom's depending
on the version.

The component under test is browser code, so it should see the browser's
storage. Binding explicitly is what makes the version matrix in this
plan meaningful rather than a source of three-way flakes."
```

---

### Task 2: Reconcile the declared engine ranges

**Files:**
- Modify: `package.json:5`
- Modify: `packages/provide-uterm-ts/package.json:9`
- Modify: any other workspace package declaring `engines`

- [ ] **Step 1: Enumerate every declaration**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -rn '"engines"' -A3 package.json packages/*/package.json | grep -E "package.json|node"
```

- [ ] **Step 2: Decide the floor**

The floor must be a version CI actually runs. Node 20 reached end of life, and
nothing in this repository has ever been tested on it, so `>=20` is a claim with
no evidence behind it.

Set every package to `>=22`, matching `.nvmrc` and the TypeScript package's
existing, more honest declaration.

If any package genuinely needs a higher floor, set it higher and say why in a
comment — but do not lower any package below what the matrix proves.

- [ ] **Step 3: Apply**

In `package.json`:

```json
    "node": ">=22",
```

- [ ] **Step 4: Verify install still resolves**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
npm ci
npm run typecheck:frontend
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add package.json packages/*/package.json
git commit -m "build: declare the Node floor CI actually proves

The root package claimed >=20 and the TypeScript package >=22, and
nothing had ever run on 20 or 21 — .nvmrc pins 22 and all eight CI setup
steps read it.

Raise the root to >=22 so the declaration matches the evidence. A floor
nothing tests is a claim, not a guarantee."
```

---

### Task 3: Add the version matrix

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Identify the job to matrix**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -n "node-version-file" .github/workflows/ci.yml
```

Eight occurrences. The one that should become a matrix is the TypeScript
build/typecheck/lint/test job — `npm-quality`. The others (asset builds, Docker
image builds, e2e) stay on `.nvmrc`: they are not testing Node compatibility and
tripling them buys nothing but CI minutes.

- [ ] **Step 2: Add the matrix**

On the `npm-quality` job:

```yaml
    strategy:
      fail-fast: false
      matrix:
        # 22 is the declared floor, 26 is current. 24 is between them: a
        # version that only breaks in the middle of the range is the one a
        # two-point matrix misses.
        node-version: ["22", "24", "26"]
```

and replace its setup step's `node-version-file` with:

```yaml
          node-version: ${{ matrix.node-version }}
```

`fail-fast: false` matters: with it true, the first red cell cancels the others
and you learn one version failed instead of which.

- [ ] **Step 3: Check the timeout**

`npm-quality` had its `timeout-minutes` raised to 20 earlier in this repository's
history after a genuine hang. The matrix does not make any single cell slower, so
20 stays correct per cell — but confirm the value is still there rather than
assuming.

Run: `grep -n -B5 -A2 "npm-quality" .github/workflows/ci.yml | grep timeout`

- [ ] **Step 4: Verify the workflow parses**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
uv run python -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text()); print('ok')"
```

Expected: `ok`.

- [ ] **Step 5: Test each version locally where possible**

Run, for each version available on the host:

```bash
cd /Volumes/data/pyv/provide-uterm
nvm use 24 && npm ci && npm run typecheck:frontend && npm run lint:frontend
nvm use 26 && npm ci && npm run typecheck:frontend && npm run lint:frontend
nvm use 22
```

Expected: PASS on each. Any failure here is a real compatibility finding, which
is the point of the matrix — record it and fix it before pushing rather than
discovering it in three CI cells at once.

- [ ] **Step 6: Push and confirm all three cells**

Expected: three green `npm-quality` cells.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run npm-quality on Node 22, 24 and 26

Every CI setup step read .nvmrc, so only Node 22 was ever exercised
while packages declared support down to 20.

Only npm-quality gets the matrix. Asset builds, image builds and e2e are
not testing Node compatibility, and tripling them costs minutes without
proving anything.

fail-fast is off: with it on, the first red cell cancels the rest and
you learn that a version failed rather than which."
```

---

## Definition of done

Per the measurement spec, CM-11 closes when:

- `npm-quality` runs green on Node 22, 24 and 26;
- every workspace package declares an `engines.node` floor the matrix proves;
- the app tests bind `localStorage` to jsdom explicitly, asserted by a test;
- `.nvmrc` and the declared floor agree.

Then update the CM-11 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- Do Task 1 first. The storage binding is the most likely source of a
  version-specific failure, and adding the matrix before fixing it produces
  three red cells with one cause — which reads as a Node compatibility problem
  rather than a test-environment one.
- Node 26 may not yet be available in `actions/setup-node`. If the cell cannot
  provision, that is a real blocker: leave the matrix at 22 and 24, and record
  in the measurement spec that 26 is pending availability rather than marking
  the finding closed. The design's own standard is that "unexplained tool
  failure or skipped provisioning is not a pass."
- If a dependency has no prebuilt binary for a newer Node's ABI, the fix is
  usually a dependency bump rather than dropping the version from the matrix.
