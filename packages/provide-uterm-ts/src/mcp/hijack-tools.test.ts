//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden, must } from "../testing/golden.ts";
import { callHijackTool, HIJACK_TOOL_NAMES, type HijackToolClient, MAX_KEYSTROKE_BYTES, toolAnswer } from "./index.ts";

interface Case {
  name: string;
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  data: unknown;
  result: Record<string, unknown>;
  calls: Array<Record<string, unknown>>;
}

interface ToolsGolden {
  max_keystroke_bytes: number;
  screen: string;
  snapshot: Record<string, unknown>;
  tools: string[];
  cases: Case[];
}

const golden = loadGolden<ToolsGolden>("mcphijacktools_golden.json");

/** A client that records what it was asked and answers to script. */
function recordingClient(ok: boolean, data: unknown) {
  const calls: Array<Record<string, unknown>> = [];
  const record = async (method: string, args: Record<string, unknown> = {}) => {
    calls.push({ method, ...args });
    return { ok, data };
  };
  const client: HijackToolClient = {
    acquire: (workerId, options) => record("acquire", { worker_id: workerId, ...options }),
    heartbeat: (workerId, hijackId, options) =>
      record("heartbeat", { worker_id: workerId, hijack_id: hijackId, ...options }),
    snapshot: (workerId, hijackId, options) =>
      record("snapshot", { worker_id: workerId, hijack_id: hijackId, ...options }),
    events: (workerId, hijackId, options) => record("events", { worker_id: workerId, hijack_id: hijackId, ...options }),
    send: (workerId, hijackId, options) => record("send", { worker_id: workerId, hijack_id: hijackId, ...options }),
    step: (workerId, hijackId) => record("step", { worker_id: workerId, hijack_id: hijackId }),
    release: (workerId, hijackId) => record("release", { worker_id: workerId, hijack_id: hijackId }),
    health: () => record("health"),
    setSessionMode: (sessionId, mode) => record("set_session_mode", { session_id: sessionId, mode }),
    setInputMode: (workerId, mode) => record("set_input_mode", { worker_id: workerId, mode }),
    disconnectWorker: (workerId) => record("disconnect_worker", { worker_id: workerId }),
  };
  return { client, calls };
}

describe("the ten tools a model can call", () => {
  it("registers the tools the reference registers", () => {
    expect([...HIJACK_TOOL_NAMES]).toEqual(golden.tools);
  });

  it("caps keystrokes where the reference caps them", () => {
    expect(MAX_KEYSTROKE_BYTES).toBe(golden.max_keystroke_bytes);
  });

  it.each(golden.cases)("$name", async (record) => {
    const { client, calls } = recordingClient(record.ok, record.data);
    const result = await callHijackTool(client, record.tool, record.args);

    expect(result).toEqual(record.result);
    expect(Object.keys(result)).toEqual(Object.keys(record.result));
    expect(calls).toEqual(record.calls);
  });
});

describe("what happens before the client is touched", () => {
  it("refuses an id that is a path without reaching at all", async () => {
    // The point of checking here: a caller-supplied segment must never reach
    // a request path, so the refusal has to come before the call, not after.
    for (const [tool, args] of [
      ["hijack_begin", { worker_id: "a/b" }],
      ["hijack_step", { worker_id: "a/b", hijack_id: "h-1" }],
      ["worker_disconnect", { worker_id: "../etc" }],
      ["session_set_mode", { session_id: "a/b", mode: "open" }],
    ] as const) {
      const { client, calls } = recordingClient(true, {});
      const result = await callHijackTool(client, tool, args as Record<string, unknown>);
      expect(result.error).toBe("invalid_id");
      expect(calls).toEqual([]);
    }
  });

  it("names whichever of two ids is bad first", async () => {
    // So a caller fixes them in the order they were given rather than being
    // told about the second while the first is still wrong.
    const { client } = recordingClient(true, {});
    const both = await callHijackTool(client, "hijack_heartbeat", { worker_id: "a/b", hijack_id: "c/d" });
    expect(both.detail).toBe("invalid worker_id: 'a/b'");
    const second = await callHijackTool(client, "hijack_heartbeat", { worker_id: "w-1", hijack_id: "c/d" });
    expect(second.detail).toBe("invalid hijack_id: 'c/d'");
  });

  it("refuses a pattern that could hang the server, without sending it", async () => {
    // The server would compile it too, so forwarding it is handing on the
    // problem rather than solving it.
    const { client, calls } = recordingClient(true, {});
    const result = await callHijackTool(client, "hijack_send", {
      worker_id: "w-1",
      hijack_id: "h-1",
      keys: "y",
      expect_regex: "(a+)+$",
    });
    expect(result.error).toBe("invalid_pattern");
    expect(calls).toEqual([]);
  });

  it("checks the ids before the pattern", async () => {
    // Both are refusals; which one a caller is told about is decided by the
    // order, and the reference checks the ids first.
    const { client } = recordingClient(true, {});
    const result = await callHijackTool(client, "hijack_send", {
      worker_id: "a/b",
      hijack_id: "h-1",
      keys: "y",
      expect_regex: "(a+)+",
    });
    expect(result.error).toBe("invalid_id");
  });

  it("caps what a model can type in one go", async () => {
    // A model that emits a megabyte of keystrokes must not put a megabyte
    // into a terminal.
    const { client, calls } = recordingClient(true, {});
    await callHijackTool(client, "hijack_send", {
      worker_id: "w-1",
      hijack_id: "h-1",
      keys: "x".repeat(MAX_KEYSTROKE_BYTES * 2),
    });
    expect((must(calls[0], "the recorded keystroke call").keys as string).length).toBe(MAX_KEYSTROKE_BYTES);
  });
});

describe("the one shape every answer takes", () => {
  it("spreads a mapping and puts anything else under a name", () => {
    // A list spread into the answer would invent fields; under `data` it
    // stays what it was.
    expect(toolAnswer(true, { a: 1 })).toEqual({ success: true, a: 1 });
    expect(toolAnswer(false, { a: 1 })).toEqual({ success: false, a: 1 });
    expect(toolAnswer(true, [1, 2])).toEqual({ success: true, data: [1, 2] });
    expect(toolAnswer(true, "text")).toEqual({ success: true, data: "text" });
    expect(toolAnswer(true, 7)).toEqual({ success: true, data: 7 });
    expect(toolAnswer(true, null)).toEqual({ success: true, data: null });
    expect(toolAnswer(true, undefined)).toEqual({ success: true, data: undefined });
  });

  it("says success first, so a body carrying its own overrides it", () => {
    // Recorded rather than corrected: the reference spreads the body after
    // the verdict, so a server answering `success` decides for the tool. It
    // reads as a mistake and is faithful to the reference, which is why it
    // is pinned here rather than left to chance.
    expect(toolAnswer(false, { success: true, status: "ok" })).toEqual({ success: true, status: "ok" });
    expect(toolAnswer(true, { success: false })).toEqual({ success: false });
  });

  it("puts success first in the order too", () => {
    // Which is what makes the override possible, and what a reader of the
    // JSON sees first.
    expect(Object.keys(toolAnswer(true, { a: 1, b: 2 }))).toEqual(["success", "a", "b"]);
  });
});

describe("reading a screen", () => {
  it("strips the escapes unless asked not to", async () => {
    // A model reading escape sequences reads noise; one reading the raw
    // stream is doing it on purpose.
    const { client } = recordingClient(true, { snapshot: golden.snapshot });
    const plain = await callHijackTool(client, "hijack_read", { worker_id: "w-1", hijack_id: "h-1" });
    expect((plain.snapshot as Record<string, string>).screen).toBe("red\nplain\nlast");
    const raw = await callHijackTool(client, "hijack_read", {
      worker_id: "w-1",
      hijack_id: "h-1",
      output: "raw",
    });
    expect((raw.snapshot as Record<string, string>).screen).toBe(golden.screen);
  });

  it("keeps only the screen unless the layout was asked for", async () => {
    const { client } = recordingClient(true, { snapshot: golden.snapshot });
    const plain = await callHijackTool(client, "hijack_read", { worker_id: "w-1", hijack_id: "h-1" });
    expect(Object.keys(plain.snapshot as object)).toEqual(["screen"]);
    const laid = await callHijackTool(client, "hijack_read", {
      worker_id: "w-1",
      hijack_id: "h-1",
      output: "rendered",
    });
    expect(Object.keys(laid.snapshot as object)).toEqual(["screen", "cursor", "cols", "rows"]);
  });

  it("reads events from the events endpoint, and never cleans them", async () => {
    // A cleaned event log would have its payloads rewritten.
    const { client, calls } = recordingClient(true, { snapshot: golden.snapshot });
    const result = await callHijackTool(client, "hijack_read", {
      worker_id: "w-1",
      hijack_id: "h-1",
      mode: "events",
    });
    expect(calls[0]?.method).toBe("events");
    expect((result.snapshot as Record<string, string>).screen).toBe(golden.screen);
  });

  it("treats any mode that is not events as a snapshot", async () => {
    // The reference compares against `"events"` alone, so anything else — a
    // typo included — reads the screen rather than failing.
    const { client, calls } = recordingClient(true, {});
    await callHijackTool(client, "hijack_read", { worker_id: "w-1", hijack_id: "h-1", mode: "nonsense" });
    expect(calls[0]?.method).toBe("snapshot");
  });

  it("leaves a refused answer alone", async () => {
    // Cleaning a failure's payload would hide what the server said about it.
    const { client } = recordingClient(false, { snapshot: golden.snapshot });
    const result = await callHijackTool(client, "hijack_read", { worker_id: "w-1", hijack_id: "h-1" });
    expect((result.snapshot as Record<string, string>).screen).toBe(golden.screen);
  });

  it("has nothing to clean when there is no snapshot", async () => {
    const { client } = recordingClient(true, { other: 1 });
    const result = await callHijackTool(client, "hijack_read", { worker_id: "w-1", hijack_id: "h-1" });
    expect(result).toEqual({ success: true, other: 1 });
  });

  it("trims to the last lines when asked", async () => {
    const { client } = recordingClient(true, { snapshot: golden.snapshot });
    const result = await callHijackTool(client, "hijack_read", {
      worker_id: "w-1",
      hijack_id: "h-1",
      tail_lines: 1,
    });
    expect((result.snapshot as Record<string, string>).screen).toBe("last");
  });

  it("leaves a snapshot that is not a mapping alone", async () => {
    // A recorded divergence. The reference reads `.get` off whatever is
    // there, so a server answering with a bare string raises inside the tool
    // and the caller sees an error rather than the answer. Passed through
    // here: a malformed answer says more than a stack trace does, and nothing
    // downstream reads it as a screen.
    const { client } = recordingClient(true, { snapshot: "not a mapping" });
    const result = await callHijackTool(client, "hijack_read", { worker_id: "w-1", hijack_id: "h-1" });
    expect(result.snapshot).toBe("not a mapping");
  });

  it("trims only when it was given a count", async () => {
    // Absent, null and anything that is not a number all mean the same thing
    // the reference's `None` means: leave the screen alone.
    for (const tail_lines of [undefined, null, "1", true, {}]) {
      const { client } = recordingClient(true, { snapshot: golden.snapshot });
      const result = await callHijackTool(client, "hijack_read", {
        worker_id: "w-1",
        hijack_id: "h-1",
        tail_lines,
      });
      expect((result.snapshot as Record<string, string>).screen).toBe("red\nplain\nlast");
    }
  });

  it("refuses a tool nobody registered", async () => {
    // Rather than answering as though it had run.
    const { client } = recordingClient(true, {});
    await expect(callHijackTool(client, "no_such_tool", {})).rejects.toThrow("unknown tool: no_such_tool");
  });
});
