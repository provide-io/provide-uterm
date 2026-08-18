//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * Guards that the test environment supplies a *working* DOM Storage.
 *
 * This exists because the failure it guards is silent here. `loadSettings` in
 * terminal-settings.ts reads localStorage inside a try/catch so a corrupt entry
 * cannot wedge startup; the same catch swallows a missing Storage, so the
 * widget quietly falls back to defaults and persistence stops working with
 * every test still green.
 *
 * It has already happened once. Node 26 defines `localStorage` on globalThis as
 * an accessor returning undefined without --localstorage-file. vitest's jsdom
 * environment does not overwrite globals that already exist, so the accessor
 * survived and `window.localStorage` was undefined too. The rest of this
 * package's suite never noticed, because those tests `vi.stubGlobal` their own
 * mock -- which is exactly why the environment itself needs asserting.
 *
 * Deliberately does not stub anything.
 */
import { beforeEach, describe, expect, it } from "vitest";

describe("test environment DOM Storage", () => {
  beforeEach(() => localStorage.clear());

  it("provides a defined localStorage global", () => {
    expect(localStorage, "no Storage in the test environment").toBeDefined();
  });

  it("exposes the same Storage on window and globalThis", () => {
    expect(window.localStorage).toBe(globalThis.localStorage);
  });

  it("round-trips a value rather than silently dropping it", () => {
    localStorage.setItem("uterm.probe", "kept");
    expect(localStorage.getItem("uterm.probe")).toBe("kept");
  });

  it("reports absent keys as null, not undefined", () => {
    expect(localStorage.getItem("uterm.absent")).toBeNull();
  });

  it("supports removeItem, clear, length and key", () => {
    localStorage.setItem("a", "1");
    localStorage.setItem("b", "2");
    expect(localStorage.length).toBe(2);
    expect(typeof localStorage.key(0)).toBe("string");

    localStorage.removeItem("a");
    expect(localStorage.getItem("a")).toBeNull();
    expect(localStorage.length).toBe(1);

    localStorage.clear();
    expect(localStorage.length).toBe(0);
  });
});
