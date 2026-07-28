//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { WorkerRegistry } from "./index.ts";

/** A stand-in for the worker state the registry stores. */
interface State {
  id: string;
}

const a: State = { id: "a" };
const b: State = { id: "b" };

describe("WorkerRegistry lookup", () => {
  it("returns undefined for a worker it does not know", () => {
    expect(new WorkerRegistry<State>().get("nope")).toBeUndefined();
  });

  it("returns the state it was given", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    expect(registry.get("w1")).toBe(a);
  });

  it("throws from require for an unknown worker", () => {
    expect(() => new WorkerRegistry<State>().require("nope")).toThrow("nope");
  });

  it("returns the state from require when it is known", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    expect(registry.require("w1")).toBe(a);
  });

  it("reports membership", () => {
    const registry = new WorkerRegistry<State>();
    expect(registry.contains("w1")).toBe(false);
    registry.put("w1", a);
    expect(registry.contains("w1")).toBe(true);
  });
});

describe("WorkerRegistry mutation", () => {
  it("replaces an existing entry on put", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    registry.put("w1", b);
    expect(registry.get("w1")).toBe(b);
    expect(registry.size).toBe(1);
  });

  it("keeps the existing entry on setDefault", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    // The point of setDefault: an attach racing a reattach must not discard
    // the live state and hand back a fresh one.
    expect(registry.setDefault("w1", b)).toBe(a);
    expect(registry.get("w1")).toBe(a);
  });

  it("inserts and returns the new entry when setDefault finds nothing", () => {
    const registry = new WorkerRegistry<State>();
    expect(registry.setDefault("w1", a)).toBe(a);
    expect(registry.get("w1")).toBe(a);
  });

  it("returns the removed state from pop", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    expect(registry.pop("w1")).toBe(a);
    expect(registry.contains("w1")).toBe(false);
  });

  it("returns undefined when popping an unknown worker", () => {
    expect(new WorkerRegistry<State>().pop("nope")).toBeUndefined();
  });

  it("reports whether discard removed anything", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    expect(registry.discard("w1")).toBe(true);
    expect(registry.discard("w1")).toBe(false);
  });
});

describe("WorkerRegistry snapshots", () => {
  it("counts its entries", () => {
    const registry = new WorkerRegistry<State>();
    expect(registry.size).toBe(0);
    registry.put("w1", a);
    registry.put("w2", b);
    expect(registry.size).toBe(2);
  });

  it("lists ids, states and pairs in insertion order", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    registry.put("w2", b);
    expect(registry.keys()).toStrictEqual(["w1", "w2"]);
    expect(registry.all()).toStrictEqual([a, b]);
    expect(registry.items()).toStrictEqual([
      ["w1", a],
      ["w2", b],
    ]);
  });

  it("returns snapshots that a later mutation does not disturb", () => {
    // Callers iterate these while the hub mutates the registry, so a live
    // view would throw or skip entries mid-iteration.
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    const keys = registry.keys();
    const states = registry.all();
    const items = registry.items();
    registry.put("w2", b);
    registry.pop("w1");
    expect(keys).toStrictEqual(["w1"]);
    expect(states).toStrictEqual([a]);
    expect(items).toStrictEqual([["w1", a]]);
  });

  it("iterates its ids", () => {
    const registry = new WorkerRegistry<State>();
    registry.put("w1", a);
    registry.put("w2", b);
    expect([...registry]).toStrictEqual(["w1", "w2"]);
  });
});
