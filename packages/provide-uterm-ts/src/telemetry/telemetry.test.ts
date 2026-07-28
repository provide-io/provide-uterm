//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { type Logger, noopLogger } from "./index.ts";

/** Every method the Logger contract requires. */
const LOG_METHODS = ["trace", "debug", "info", "warn", "error"] as const;

describe("noopLogger", () => {
  it("satisfies the Logger contract", () => {
    const log: Logger = noopLogger;
    for (const method of LOG_METHODS) {
      expect(typeof log[method]).toBe("function");
    }
    expect(typeof log.child).toBe("function");
  });

  it("swallows every call without throwing", () => {
    for (const method of LOG_METHODS) {
      expect(() => noopLogger[method]({ event: "x" }, "message")).not.toThrow();
    }
  });

  it("accepts a call with no message argument", () => {
    expect(() => noopLogger.info({ event: "x" })).not.toThrow();
  });

  it("returns itself as its own child, so binding fields costs nothing", () => {
    expect(noopLogger.child({ worker_id: "w1" })).toBe(noopLogger);
  });
});

describe("Logger contract", () => {
  it("is satisfied by a recording test double", () => {
    // The shape library modules depend on. It matches the interface
    // @provide-io/telemetry exports, so swapping in the real logger later is
    // a type-level no-op for every caller.
    const records: Array<{ level: string; obj: Record<string, unknown>; msg?: string }> = [];
    const make = (bound: Record<string, unknown>): Logger => ({
      trace: (obj, msg) => records.push({ level: "trace", obj: { ...bound, ...obj }, ...(msg ? { msg } : {}) }),
      debug: (obj, msg) => records.push({ level: "debug", obj: { ...bound, ...obj }, ...(msg ? { msg } : {}) }),
      info: (obj, msg) => records.push({ level: "info", obj: { ...bound, ...obj }, ...(msg ? { msg } : {}) }),
      warn: (obj, msg) => records.push({ level: "warn", obj: { ...bound, ...obj }, ...(msg ? { msg } : {}) }),
      error: (obj, msg) => records.push({ level: "error", obj: { ...bound, ...obj }, ...(msg ? { msg } : {}) }),
      child: (bindings) => make({ ...bound, ...bindings }),
    });

    const log = make({});
    log.info({ event: "started" }, "up");
    log.child({ worker_id: "w1" }).warn({ event: "slow" });

    expect(records).toStrictEqual([
      { level: "info", obj: { event: "started" }, msg: "up" },
      { level: "warn", obj: { worker_id: "w1", event: "slow" } },
    ]);
  });
});
