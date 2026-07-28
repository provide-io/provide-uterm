//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, describe, expect, it, vi } from "vitest";
import { getLogger, type LogRecord, setLogSink } from "./index.ts";

/** Capture everything emitted while `run` executes. */
function capture(run: () => void): LogRecord[] {
  const records: LogRecord[] = [];
  const restore = setLogSink((record) => records.push(record));
  try {
    run();
  } finally {
    restore();
  }
  return records;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("getLogger", () => {
  it("carries the whole level surface", () => {
    const log = getLogger("uterm.test");
    for (const method of ["trace", "debug", "info", "warn", "error"] as const) {
      expect(typeof log[method]).toBe("function");
    }
    expect(typeof log.child).toBe("function");
  });

  it("emits the logger name, level, fields and message", () => {
    const records = capture(() => {
      getLogger("uterm.test").info({ event: "started", port: 8780 }, "listening");
    });
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({
      name: "uterm.test",
      level: "info",
      msg: "listening",
      fields: { event: "started", port: 8780 },
    });
  });

  it("emits without a message when none is given", () => {
    const records = capture(() => {
      getLogger("uterm.test").warn({ event: "slow" });
    });
    expect(records[0]?.msg).toBeUndefined();
  });

  it("stamps each record with the current time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_700_000_000_500));
    const records = capture(() => {
      getLogger("uterm.test").info({ event: "x" });
    });
    expect(records[0]?.ts).toBe(1_700_000_000.5);
  });

  it("routes every level under its own name", () => {
    const records = capture(() => {
      const log = getLogger("uterm.test");
      log.trace({ event: "a" });
      log.debug({ event: "b" });
      log.info({ event: "c" });
      log.warn({ event: "d" });
      log.error({ event: "e" });
    });
    expect(records.map((r) => r.level)).toStrictEqual(["trace", "debug", "info", "warn", "error"]);
  });
});

describe("child loggers", () => {
  it("merges bound fields into every record", () => {
    const records = capture(() => {
      getLogger("uterm.test").child({ worker_id: "w1" }).info({ event: "x" });
    });
    expect(records[0]?.fields).toStrictEqual({ worker_id: "w1", event: "x" });
  });

  it("lets a call field override a bound one", () => {
    const records = capture(() => {
      getLogger("uterm.test").child({ scope: "outer" }).info({ scope: "inner" });
    });
    expect(records[0]?.fields).toStrictEqual({ scope: "inner" });
  });

  it("accumulates bindings across nested children", () => {
    const records = capture(() => {
      getLogger("uterm.test").child({ a: 1 }).child({ b: 2 }).info({ c: 3 });
    });
    expect(records[0]?.fields).toStrictEqual({ a: 1, b: 2, c: 3 });
  });

  it("keeps the parent's name", () => {
    const records = capture(() => {
      getLogger("uterm.test").child({ a: 1 }).info({});
    });
    expect(records[0]?.name).toBe("uterm.test");
  });

  it("does not leak a child's bindings back to its parent", () => {
    const records = capture(() => {
      const parent = getLogger("uterm.test");
      parent.child({ a: 1 }).info({ event: "child" });
      parent.info({ event: "parent" });
    });
    expect(records[1]?.fields).toStrictEqual({ event: "parent" });
  });
});

describe("the default sink", () => {
  it("writes one JSON line to stderr", () => {
    // Asserted directly because it is the behaviour an operator sees when
    // nothing has configured a sink, which is the common case in production.
    const written: string[] = [];
    const original = process.stderr.write.bind(process.stderr);
    const spy = vi.spyOn(process.stderr, "write").mockImplementation(((chunk: string | Uint8Array) => {
      written.push(String(chunk));
      return true;
    }) as typeof original);
    try {
      getLogger("uterm.test").info({ event: "x" }, "hello");
    } finally {
      spy.mockRestore();
    }
    expect(written).toHaveLength(1);
    const line = written[0] as string;
    expect(line.endsWith("\n")).toBe(true);
    expect(JSON.parse(line)).toMatchObject({ name: "uterm.test", level: "info", msg: "hello" });
  });
});

describe("setLogSink", () => {
  it("returns a restore function that puts the previous sink back", () => {
    const first: LogRecord[] = [];
    const restoreFirst = setLogSink((record) => first.push(record));
    const second: LogRecord[] = [];
    const restoreSecond = setLogSink((record) => second.push(record));
    getLogger("uterm.test").info({ event: "x" });
    restoreSecond();
    getLogger("uterm.test").info({ event: "y" });
    restoreFirst();
    expect(second.map((r) => r.fields.event)).toStrictEqual(["x"]);
    expect(first.map((r) => r.fields.event)).toStrictEqual(["y"]);
  });
});
