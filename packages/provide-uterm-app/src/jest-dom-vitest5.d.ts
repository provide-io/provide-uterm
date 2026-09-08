//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// @testing-library/jest-dom augments vitest's `Assertion` as `Assertion<T = any>`,
// the shape vitest carried through 4.x. vitest 5 declares
// `Assertion<R extends void | Promise<void> = void, T = unknown>`, and TypeScript
// merges two declarations of an interface only when their type parameter lists
// match exactly. The package's augmentation therefore does not merge, and every
// jest-dom matcher reads as missing from `expect(...)`.
//
// Declaring the same matchers against vitest 5's parameter list restores them.
// Delete this file once jest-dom ships an augmentation matching vitest 5; two
// merging declarations of the same matchers are what TypeScript reports as a
// duplicate, so it cannot silently outlive its purpose.
import type { TestingLibraryMatchers } from "@testing-library/jest-dom/matchers";

declare module "vitest" {
  interface Assertion<R extends void | Promise<void> = void, T = unknown>
    extends TestingLibraryMatchers<unknown, R> {}
}
