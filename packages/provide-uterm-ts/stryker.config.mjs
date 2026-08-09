//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// StrykerJS configuration for a curated mutation-testing perimeter.
//
// Why a perimeter instead of `src/**/*.ts`: this package is ~47k LOC with a
// vitest suite already held at 100% line/branch coverage. Coverage proves a
// line executed, not that a test asserts on its result. Mutating all ~47k
// lines would take many hours per run and mostly scores glue/plumbing code
// that isn't security- or correctness-critical. Instead we mirror the
// Python (mutmut, root MUTATION_PATTERNS.md) and Go (gremlins,
// provide-uterm-go/ci/mutation_gate.py) ports: enumerate only the files
// where a silently-wrong mutation is dangerous or expensive --- security
// surfaces, wire-format/state-machine code, and boundary arithmetic. See
// the perimeter rationale in this package's mutation report (reported back
// to the requester) for the file-by-file "why".
export default {
  packageManager: "npm",
  testRunner: "vitest",
  // "json" gives a full, machine-readable survivor list — the clear-text
  // reporter truncates its per-mutant diffs on a run this size, which is what
  // let one undocumented survivor (validators.ts, a second location) hide
  // behind a documented one at another line in the same file during review.
  reporters: ["html", "json", "clear-text", "progress"],
  htmlReporter: {
    fileName: "reports/mutation/mutation.html",
  },
  jsonReporter: {
    fileName: "reports/mutation/mutation.json",
  },

  // Perimeter: egress/CIDR + webhook-destination guards, the hub state
  // store, the hosted-server bootstrap factory, server-config schema +
  // validators + posture/security-headers resolution, and the rate
  // limiter's boundary arithmetic. Excludes anything needing a real
  // WebSocket/PTY/child-process (transports, connectors, the actual hub
  // wiring beyond the store) --- matching why Python/Go exclude live-I/O
  // code from their perimeters; that code is exercised by integration/e2e
  // tests, not unit-level mutation testing.
  mutate: [
    "src/egress/egress.ts",
    "src/egress/webhook-url.ts",
    "src/hub/store.ts",
    "src/server/bootstrap.ts",
    "src/serverconfig/defaults.ts",
    "src/serverconfig/loader.ts",
    "src/serverconfig/posture.ts",
    "src/serverconfig/profiles.ts",
    "src/serverconfig/schema-fields.ts",
    "src/serverconfig/schema.ts",
    "src/serverconfig/security-headers.ts",
    "src/serverconfig/server-defaults.ts",
    "src/serverconfig/validators.ts",
    "src/ratelimit/ratelimit.ts",
  ],

  vitest: {
    configFile: "vitest.config.ts",
  },

  // NOTE on the typescript-checker plugin: intentionally NOT enabled here.
  // This package pins `typescript@7.0.2`, which is the new Go-native
  // compiler ("tsgo") preview -- its npm package no longer exports the
  // classic Compiler API (`ts.createProgram`, `ts.transpileModule`,
  // `ts.parseConfigFileTextToJson`, etc; `require("typescript")` here
  // resolves only { version, versionMajorMinor }). Both
  // `@stryker-mutator/typescript-checker` (9.6.1) and even Stryker core's
  // own tsconfig-rewriting preprocessor hard-depend on that classic API and
  // crash (`ts.parseConfigFileTextToJson is not a function`) the moment a
  // real `tsconfig.json` is in the sandbox. Vitest itself doesn't need the
  // classic API either (it type-strips via esbuild/oxc), so the only
  // casualty is cheap pre-filtering of type-error mutants -- every mutant
  // still runs through the real vitest suite, which catches the same
  // survivors, just at test-run cost instead of a fast type-check. Revisit
  // once either typescript-checker ships tsgo support or the classic API
  // returns.
  tsconfigFile: "tsconfig.stryker-unused.json",

  // Every perimeter mutant must be attributable to a killing test; anything
  // ignored below (equivalent mutants) is enumerated with a one-line reason
  // in mutation_equivalents.toml, not silently dropped here.
  ignoreStatic: true,

  // inPlace (mutate the working tree, restore afterwards) rather than Stryker's
  // default sandbox copy. The sandbox contains only this package, but
  // src/fanout/security-scenarios.test.ts loads the SHARED cross-language corpus
  // at <repo-root>/spec/fanout_security_scenarios.json, resolved four levels up
  // from import.meta.dirname. Inside the sandbox that path does not exist, the
  // dry run fails with ENOENT, and Stryker aborts before mutating anything
  // ("There were failed tests in the initial test run") — which is why this gate
  // had never completed a run. Stryker restores the files when it finishes; CI
  // runners are ephemeral, and a local interrupt is recoverable with `git
  // checkout`. Revisit if Stryker ever supports including paths above cwd.
  inPlace: true,
  concurrency: 4,
  timeoutMS: 15000,
  timeoutFactor: 3,
};
