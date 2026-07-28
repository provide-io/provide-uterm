//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  coerceWorkerStatusFrame,
  type Frame,
  makeAnalysisFrame,
  makeErrorFrame,
  makeHeartbeatAckFrame,
  makeHelloFrame,
  makeHijackStateFrame,
  makePongFrame,
  makeTermFrame,
  makeWorkerConnectedFrame,
  makeWorkerDisconnectedFrame,
} from "./index.ts";

interface FramesGolden {
  ts: number;
  frames: Array<{ name: string; frame: Frame }>;
}

const golden = loadGolden<FramesGolden>("hubframes_golden.json");
const TS = golden.ts;

/** The frame the corpus recorded under one name. */
function recorded(name: string): Frame {
  const found = golden.frames.find((entry) => entry.name === name);
  if (found === undefined) {
    throw new Error(`no recorded frame named ${name}`);
  }
  return found.frame;
}

/** Every builder, keyed by the name the corpus recorded it under. */
const BUILDERS: Record<string, () => Frame> = {
  "an error": () => makeErrorFrame("something went wrong"),
  "an error with no message": () => makeErrorFrame(""),
  "a pong": () => makePongFrame(TS),
  "a heartbeat ack": () => makeHeartbeatAckFrame(TS + 30, TS),
  "a worker connected": () => makeWorkerConnectedFrame("w1", TS),
  "a worker disconnected": () => makeWorkerDisconnectedFrame("w1", TS),
  "terminal output": () => makeTermFrame("hello", TS),
  "terminal output that is empty": () => makeTermFrame("", TS),
  "an analysis": () => makeAnalysisFrame("done", { x: 1 }, TS),
  "an analysis with no raw": () => makeAnalysisFrame("done", null, TS),
  "a hijack in progress": () => makeHijackStateFrame(true, "u1", TS + 60, "hijack"),
  "no hijack": () => makeHijackStateFrame(false, undefined, undefined, "open"),
  "a hello": () => makeHelloFrame({ worker_id: "w1" }),
  "a hello with capabilities": () => makeHelloFrame({ worker_id: "w1", replay_supported: true }),
  "a hello overriding a default": () => makeHelloFrame({ mcp_supported: false }),
  "a hello overriding its type": () => makeHelloFrame({ type: "not_hello" }),
  "a worker status": () => coerceWorkerStatusFrame({ state: "running", ts: TS }),
  "a status with no type": () => coerceWorkerStatusFrame({ state: "running", ts: TS }),
  "a status naming its own type": () => coerceWorkerStatusFrame({ type: "custom", ts: TS }),
  "an empty status": () => coerceWorkerStatusFrame({}, () => TS),
};

describe("the frames the hub sends", () => {
  it.each(golden.frames)("$name", (record) => {
    const build = BUILDERS[record.name];
    expect(build).toBeDefined();
    expect((build as () => Frame)()).toEqual(record.frame);
  });

  it("puts the fields in the order the wire expects", () => {
    // A frame's field order is what a byte-for-byte comparison downstream
    // sees, so it is asserted rather than left to a deep-equality check.
    expect(Object.keys(makeTermFrame("hello", TS))).toEqual(Object.keys(recorded("terminal output")));
    expect(Object.keys(makeHelloFrame({ worker_id: "w1" }))).toEqual(Object.keys(recorded("a hello")));
  });
});

describe("whether an absent field survives", () => {
  it("keeps a null analysis result", () => {
    // The frontend reads `raw` directly: an analysis that produced nothing is
    // a different thing from a frame that forgot to say.
    const frame = makeAnalysisFrame("done", null, TS);
    expect("raw" in frame).toBe(true);
    expect(frame.raw).toBeNull();
  });

  it("keeps both nulls on a hijack state", () => {
    // A browser reads `owner` and `lease_expires_at` straight off the frame,
    // so a session with no owner has to say so rather than leave the field
    // out and be read as unchanged.
    const frame = makeHijackStateFrame(false, undefined, undefined, "open");
    expect(frame.owner).toBeNull();
    expect(frame.lease_expires_at).toBeNull();
    expect(Object.keys(frame)).toEqual(Object.keys(recorded("no hijack")));
  });

  it("carries an empty message rather than dropping it", () => {
    expect(makeErrorFrame("")).toEqual({ type: "error", message: "" });
  });
});

describe("stamping a frame", () => {
  it("takes the time a caller supplied", () => {
    expect(makePongFrame(TS).ts).toBe(TS);
    expect(makeTermFrame("x", TS).ts).toBe(TS);
  });

  it("reads the clock when a caller supplies none", () => {
    const frame = makePongFrame(undefined, () => 42);
    expect(frame.ts).toBe(42);
  });

  it("stamps in seconds, not milliseconds", () => {
    // Every timestamp on this wire is seconds since the epoch; a frame in
    // milliseconds would read as a date thousands of years out.
    const before = Date.now() / 1000;
    const stamped = makePongFrame().ts as number;
    expect(stamped).toBeGreaterThanOrEqual(before - 1);
    expect(stamped).toBeLessThan(before + 60);
  });

  it("stamps a status frame that carries no time", () => {
    expect(coerceWorkerStatusFrame({}, () => 42).ts).toBe(42);
  });

  it("keeps a status frame's own time", () => {
    expect(coerceWorkerStatusFrame({ ts: TS }, () => 42).ts).toBe(TS);
  });
});

describe("a frame a caller composed", () => {
  it("lets a hello carry any capability", () => {
    // The set grows, and a builder that had to be edited for each one would
    // be edited late.
    const frame = makeHelloFrame({ worker_id: "w1", replay_supported: true, gui_supported: true });
    expect(frame.replay_supported).toBe(true);
    expect(frame.gui_supported).toBe(true);
  });

  it("defaults two capabilities without overriding them", () => {
    expect(makeHelloFrame({}).mcp_supported).toBe(true);
    expect(makeHelloFrame({}).vnc_supported).toBe(true);
    expect(makeHelloFrame({ mcp_supported: false }).mcp_supported).toBe(false);
  });

  it("lets a caller override even the type", () => {
    // Faithful to the reference, and worth knowing rather than discovering:
    // the caller's fields are applied over the type, not under it.
    expect(makeHelloFrame({ type: "not_hello" }).type).toBe("not_hello");
  });

  it("leaves a worker's own status fields alone", () => {
    // The worker composed it; only what is missing is supplied, and a worker
    // naming its own type is not corrected.
    expect(coerceWorkerStatusFrame({ type: "custom", ts: TS })).toEqual({ type: "custom", ts: TS });
    expect(coerceWorkerStatusFrame({ state: "running", ts: TS }).state).toBe("running");
  });

  it("does not mutate what it was given", () => {
    // The caller may still be holding it.
    const payload: Frame = { state: "running" };
    coerceWorkerStatusFrame(payload, () => TS);
    expect(payload).toEqual({ state: "running" });

    const hello: Frame = { worker_id: "w1" };
    makeHelloFrame(hello);
    expect(hello).toEqual({ worker_id: "w1" });
  });
});
