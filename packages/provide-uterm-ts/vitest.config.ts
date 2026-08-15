//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    // "hanging-process" stays silent on a normal run and names what kept the
    // process alive when one does not exit. A synchronous call that blocks --
    // the last one to do this was a recursive mkdir under /proc -- cannot be
    // cut short by testTimeout, so the file never reports and the run looks
    // identical to being merely slow until the CI job hits its own limit.
    reporters: ["default", "hanging-process"],
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.ts", "src/**/*.tsx"],
      // Barrels are pure re-exports, `testing/` is test-only scaffolding, and
      // `benchmark.ts` files are harnesses a developer runs by hand to get a
      // number rather than library code anything imports. Everything else in
      // `src/` is held at 100%.
      //
      // The benchmark exclusion matches the Go package's, which drops
      // `benchmarks/` from its coverage denominator for the same reason: at a
      // 100% threshold, the alternative is tests asserting that a benchmark
      // printed something, which measures nothing and prices every new
      // benchmark at the cost of fake tests. They are still type-checked and
      // linted, so they cannot rot unnoticed.
      exclude: [
        "src/**/*.test.ts",
        "src/**/*.test.tsx",
        "src/**/index.ts",
        "src/testing/**",
        "src/**/benchmark.ts",
      ],
      thresholds: {
        lines: 100,
        branches: 100,
        functions: 100,
        statements: 100,
      },
    },
  },
});
