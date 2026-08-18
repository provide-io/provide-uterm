//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { JSDOM } from "jsdom";

// Node 26 defines `localStorage` on globalThis as an accessor that yields
// undefined unless the process was started with --localstorage-file. vitest's
// jsdom environment skips populating globals that already exist, so it leaves
// Node's accessor in place -- and because `window === globalThis` under that
// environment, `window.localStorage` is the same undefined value. Nothing in
// the DOM environment ends up providing Storage at all.
//
// This package is why that matters beyond a crash: terminal-settings.ts wraps
// its read in try/catch, so a missing Storage does not fail loudly -- it
// silently returns defaults and persistence stops working. See
// storage-environment.test.ts, which asserts the environment out loud.
//
// `sessionStorage` needs no equivalent: Node 26 provides a working in-memory one.
Object.defineProperty(globalThis, "localStorage", {
  value: new JSDOM("", { url: "http://localhost" }).window.localStorage,
  configurable: true,
  writable: true,
});
