//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import "@testing-library/jest-dom";

// Node >= 24 defines a global `localStorage` accessor that returns undefined
// unless the process was started with --localstorage-file, and it shadows the
// one jsdom would otherwise install -- `window === globalThis` under the jsdom
// environment, so there is no second copy to fall back to. The repo pins node
// 22 (.nvmrc), where that global does not exist and jsdom's survives, so CI is
// green and only a contributor on a newer runtime sees it: every test touching
// storage dies with "Cannot read properties of undefined (reading 'clear')",
// which names neither node nor jsdom and reads like a broken test.
//
// Install a spec-shaped Storage when the ambient one is unusable, so the suite
// depends on the environment vitest was configured with rather than on which
// node happens to be first on PATH.
const ambientStorage = (globalThis as { localStorage?: Storage }).localStorage;

if (!ambientStorage) {
  const entries = new Map<string, string>();
  const storage: Storage = {
    get length(): number {
      return entries.size;
    },
    clear(): void {
      entries.clear();
    },
    getItem(key: string): string | null {
      // Web Storage returns null for an absent key, never undefined; callers
      // branch on `=== null`, so a Map miss must not leak through as-is.
      return entries.has(String(key)) ? (entries.get(String(key)) as string) : null;
    },
    key(index: number): string | null {
      return [...entries.keys()][index] ?? null;
    },
    removeItem(key: string): void {
      entries.delete(String(key));
    },
    setItem(key: string, value: string): void {
      // Both arguments are stringified by the real API, so a caller storing a
      // number reads back "1", not 1.
      entries.set(String(key), String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get: () => storage,
  });
}
