//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it, vi } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  coerceWorkerStatusFrame,
  makeAnalysisFrame,
  makeErrorFrame,
  makeHeartbeatAckFrame,
  makeHelloFrame,
  makeHijackStateFrame,
  makePongFrame,
  makeSnapshotFrame,
  makeTermFrame,
  makeWorkerConnectedFrame,
  makeWorkerDisconnectedFrame,
} from "./index.ts";

interface FramesGolden {
  ts: number;
  error: Array<{ message: string; frame: Record<string, unknown> }>;
  pong: Array<{ ts: number; frame: Record<string, unknown> }>;
  heartbeat_ack: Array<{ lease_expires_at: number; ts: number; frame: Record<string, unknown> }>;
  worker_connected: Array<{ worker_id: string; ts: number; frame: Record<string, unknown> }>;
  worker_disconnected: Array<{ worker_id: string; ts: number; frame: Record<string, unknown> }>;
  term: Array<{ data: string; ts: number; frame: Record<string, unknown> }>;
  snapshot: Array<{ name: string; kwargs: Record<string, unknown>; frame: Record<string, unknown> }>;
  analysis: Array<{ formatted: string; raw: unknown; ts: number; frame: Record<string, unknown> }>;
  hijack_state: Array<{
    hijacked: boolean;
    owner: string | null;
    lease_expires_at: number | null;
    input_mode: string;
    frame: Record<string, unknown>;
  }>;
  hello: Array<{ payload: Record<string, unknown>; frame: Record<string, unknown> }>;
  worker_status: Array<{ payload: Record<string, unknown>; frame: Record<string, unknown> }>;
}

const golden = loadGolden<FramesGolden>("frames_golden.json");
const TS = golden.ts;

describe("makeErrorFrame", () => {
  it("carries the message", () => {
    expect(makeErrorFrame("boom")).toStrictEqual({ type: "error", message: "boom" });
  });

  it("keeps an empty message", () => {
    expect(makeErrorFrame("")).toStrictEqual({ type: "error", message: "" });
  });
});

describe("timestamped frames", () => {
  it("stamps the supplied timestamp", () => {
    expect(makePongFrame({ ts: TS })).toStrictEqual({ type: "pong", ts: TS });
    expect(makeTermFrame("x", { ts: TS })).toStrictEqual({ type: "term", data: "x", ts: TS });
    expect(makeWorkerConnectedFrame("w1", { ts: TS })).toStrictEqual({
      type: "worker_connected",
      worker_id: "w1",
      ts: TS,
    });
    expect(makeWorkerDisconnectedFrame("w1", { ts: TS })).toStrictEqual({
      type: "worker_disconnected",
      worker_id: "w1",
      ts: TS,
    });
  });

  it("defaults every timestamp to the current time in seconds", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date(1_700_000_000_500));
      const expected = 1_700_000_000.5;
      expect(makePongFrame().ts).toBe(expected);
      expect(makeTermFrame("x").ts).toBe(expected);
      expect(makeHeartbeatAckFrame(1).ts).toBe(expected);
      expect(makeWorkerConnectedFrame("w1").ts).toBe(expected);
      expect(makeWorkerDisconnectedFrame("w1").ts).toBe(expected);
      expect(makeAnalysisFrame({ formatted: "f", raw: null }).ts).toBe(expected);
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves high latin-1 shim characters in terminal data", () => {
    expect(makeTermFrame("\xff\xfe", { ts: TS }).data).toBe("\xff\xfe");
  });
});

describe("makeHeartbeatAckFrame", () => {
  it("carries the lease expiry", () => {
    expect(makeHeartbeatAckFrame(TS + 30, { ts: TS })).toStrictEqual({
      type: "heartbeat_ack",
      lease_expires_at: TS + 30,
      ts: TS,
    });
  });

  it("keeps a zero lease expiry rather than dropping it", () => {
    expect(makeHeartbeatAckFrame(0, { ts: TS })).toHaveProperty("lease_expires_at", 0);
  });
});

describe("makeSnapshotFrame", () => {
  const base = {
    screen: "hello",
    cursor: { x: 1, y: 2 },
    cols: 80,
    rows: 24,
    screenHash: "deadbeef",
    cursorAtEnd: true,
    hasTrailingSpace: false,
    promptDetected: null,
    ts: TS,
  };

  it("keeps a null prompt on the wire", () => {
    // The frontend reads this field directly, so dropping it would change
    // behaviour rather than just size.
    expect(makeSnapshotFrame(base)).toHaveProperty("prompt_detected", null);
  });

  it("keeps a null raw tail on the wire", () => {
    expect(makeSnapshotFrame(base)).toHaveProperty("raw_tail", null);
  });

  it("carries a raw tail when supplied", () => {
    expect(makeSnapshotFrame({ ...base, rawTail: "tail" })).toHaveProperty("raw_tail", "tail");
  });

  it("carries a detected prompt", () => {
    const detected = { kind: "command", confidence: 0.75 };
    expect(makeSnapshotFrame({ ...base, promptDetected: detected })).toHaveProperty("prompt_detected", detected);
  });
});

describe("makeAnalysisFrame", () => {
  it("serialises a null raw payload rather than dropping it", () => {
    expect(makeAnalysisFrame({ formatted: "f", raw: null, ts: TS })).toStrictEqual({
      type: "analysis",
      formatted: "f",
      raw: null,
      ts: TS,
    });
  });

  it("carries a structured raw payload", () => {
    expect(makeAnalysisFrame({ formatted: "f", raw: { a: 1 }, ts: TS }).raw).toStrictEqual({ a: 1 });
  });
});

describe("makeHijackStateFrame", () => {
  it("keeps a null owner and lease expiry", () => {
    expect(
      makeHijackStateFrame({ hijacked: false, owner: null, leaseExpiresAt: null, inputMode: "read_only" }),
    ).toStrictEqual({
      type: "hijack_state",
      hijacked: false,
      owner: null,
      lease_expires_at: null,
      input_mode: "read_only",
    });
  });

  it("carries an owner and lease expiry when held", () => {
    expect(
      makeHijackStateFrame({ hijacked: true, owner: "user:alice", leaseExpiresAt: TS, inputMode: "read_write" }),
    ).toStrictEqual({
      type: "hijack_state",
      hijacked: true,
      owner: "user:alice",
      lease_expires_at: TS,
      input_mode: "read_write",
    });
  });

  it("carries no timestamp, unlike the other frames", () => {
    expect(
      makeHijackStateFrame({ hijacked: false, owner: null, leaseExpiresAt: null, inputMode: "read_only" }),
    ).not.toHaveProperty("ts");
  });
});

describe("makeHelloFrame", () => {
  it("defaults both capability flags to true", () => {
    expect(makeHelloFrame()).toStrictEqual({ type: "hello", mcp_supported: true, vnc_supported: true });
  });

  it("lets a caller turn a capability off", () => {
    expect(makeHelloFrame({ mcp_supported: false })).toHaveProperty("mcp_supported", false);
    expect(makeHelloFrame({ vnc_supported: false })).toHaveProperty("vnc_supported", false);
  });

  it("passes an unmodelled capability flag through", () => {
    expect(makeHelloFrame({ resume_supported: true })).toHaveProperty("resume_supported", true);
  });

  it("does not let a caller override the frame type", () => {
    expect(makeHelloFrame({ type: "other" }).type).toBe("hello");
  });
});

describe("coerceWorkerStatusFrame", () => {
  it("supplies the default type and timestamp", () => {
    expect(coerceWorkerStatusFrame({ ts: TS })).toStrictEqual({ type: "status", ts: TS });
  });

  it("leaves a caller-supplied type alone", () => {
    expect(coerceWorkerStatusFrame({ type: "custom", ts: TS }).type).toBe("custom");
  });

  it("stamps a timestamp when the payload has none", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date(1_700_000_000_500));
      expect(coerceWorkerStatusFrame({ state: "idle" })).toStrictEqual({
        type: "status",
        state: "idle",
        ts: 1_700_000_000.5,
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not mutate the caller's payload", () => {
    const payload = { state: "idle" };
    coerceWorkerStatusFrame(payload);
    expect(payload).toStrictEqual({ state: "idle" });
  });
});

describe("differential parity with CPython", () => {
  it("matches every error frame", () => {
    for (const record of golden.error) {
      expect(makeErrorFrame(record.message)).toStrictEqual(record.frame);
    }
  });

  it("matches every timestamped frame", () => {
    for (const record of golden.pong) {
      expect(makePongFrame({ ts: record.ts })).toStrictEqual(record.frame);
    }
    for (const record of golden.heartbeat_ack) {
      expect(makeHeartbeatAckFrame(record.lease_expires_at, { ts: record.ts })).toStrictEqual(record.frame);
    }
    for (const record of golden.worker_connected) {
      expect(makeWorkerConnectedFrame(record.worker_id, { ts: record.ts })).toStrictEqual(record.frame);
    }
    for (const record of golden.worker_disconnected) {
      expect(makeWorkerDisconnectedFrame(record.worker_id, { ts: record.ts })).toStrictEqual(record.frame);
    }
    for (const record of golden.term) {
      expect(makeTermFrame(record.data, { ts: record.ts })).toStrictEqual(record.frame);
    }
  });

  it("matches every snapshot frame, null fields included", () => {
    for (const record of golden.snapshot) {
      const kwargs = record.kwargs as {
        screen: string;
        cursor: Record<string, number>;
        cols: number;
        rows: number;
        screen_hash: string;
        cursor_at_end: boolean;
        has_trailing_space: boolean;
        prompt_detected: Record<string, unknown> | null;
        ts: number;
        raw_tail?: string | null;
      };
      expect({
        name: record.name,
        frame: makeSnapshotFrame({
          screen: kwargs.screen,
          cursor: kwargs.cursor,
          cols: kwargs.cols,
          rows: kwargs.rows,
          screenHash: kwargs.screen_hash,
          cursorAtEnd: kwargs.cursor_at_end,
          hasTrailingSpace: kwargs.has_trailing_space,
          promptDetected: kwargs.prompt_detected,
          ts: kwargs.ts,
          rawTail: kwargs.raw_tail ?? null,
        }),
      }).toStrictEqual({ name: record.name, frame: record.frame });
    }
    expect(golden.snapshot.length).toBeGreaterThan(4);
  });

  it("matches every analysis frame", () => {
    for (const record of golden.analysis) {
      expect(makeAnalysisFrame({ formatted: record.formatted, raw: record.raw, ts: record.ts })).toStrictEqual(
        record.frame,
      );
    }
  });

  it("matches every hijack-state frame", () => {
    for (const record of golden.hijack_state) {
      expect(
        makeHijackStateFrame({
          hijacked: record.hijacked,
          owner: record.owner,
          leaseExpiresAt: record.lease_expires_at,
          inputMode: record.input_mode,
        }),
      ).toStrictEqual(record.frame);
    }
  });

  it("matches every hello frame", () => {
    for (const record of golden.hello) {
      expect(makeHelloFrame(record.payload)).toStrictEqual(record.frame);
    }
    expect(golden.hello.length).toBeGreaterThan(4);
  });

  it("matches every worker-status frame", () => {
    for (const record of golden.worker_status) {
      expect(coerceWorkerStatusFrame({ ...record.payload, ts: TS })).toStrictEqual(record.frame);
    }
  });
});
