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
      // Barrels are pure re-exports and `testing/` is test-only scaffolding;
      // everything else in `src/` is held at 100%.
      exclude: ["src/**/*.test.ts", "src/**/*.test.tsx", "src/**/index.ts", "src/testing/**"],
      thresholds: {
        lines: 100,
        branches: 100,
        functions: 100,
        statements: 100,
      },
    },
  },
});
