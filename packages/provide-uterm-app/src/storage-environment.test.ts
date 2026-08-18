//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * Guards that the test environment supplies a *working* DOM Storage.
 *
 * ConnectPage persists recent endpoints in localStorage, and its suite calls
 * `localStorage.clear()` in beforeEach. When the environment has no Storage
 * that surfaces as eight `Cannot read properties of undefined (reading
 * 'clear')` failures pointing at a beforeEach hook -- which says nothing about
 * the actual cause.
 *
 * It has already happened once. Node 26 defines `localStorage` on globalThis as
 * an accessor returning undefined without --localstorage-file. vitest's jsdom
 * environment does not overwrite globals that already exist, so the accessor
 * survived and `window.localStorage` was undefined too. src/test-setup.ts now
 * binds a real jsdom Storage; this asserts that it worked, so the next runtime
 * to claim the global fails here with a plain message instead of scattering
 * TypeErrors across unrelated suites.
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
