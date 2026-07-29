//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  callSessionTool,
  GUI_TOOL_NAMES,
  SESSION_TOOL_NAMES,
  type SessionToolClient,
  SUBSCRIBE_MAX_EVENTS,
  SUBSCRIBE_MAX_SECONDS,
  WATCH_MAX_EVENTS,
  WATCH_MAX_SECONDS,
} from "./index.ts";

interface Case {
  name: string;
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  data: unknown;
  result: Record<string, unknown>;
  calls: Array<Record<string, unknown>>;
  /** Set where this port answers differently on purpose. */
  diverges: boolean;
}

interface SessionToolsGolden {
  screen: string;
  snapshot: Record<string, unknown>;
  events: { events: unknown[] };
  session_tools: string[];
  gui_tools: string[];
  cases: Case[];
}

const golden = loadGolden<SessionToolsGolden>("mcpsessiontools_golden.json");

/** A client that records what it was asked and answers to script. */
function recordingClient(ok: boolean, data: unknown) {
  const calls: Array<Record<string, unknown>> = [];
  const record = async (method: string, args: Record<string, unknown> = {}) => {
    calls.push({ method, ...args });
    return { ok, data };
  };
  const client: SessionToolClient = {
    listSessions: () => record("list_sessions"),
    getSession: (sessionId) => record("get_session", { session_id: sessionId }),
    sessionSnapshot: (sessionId) => record("session_snapshot", { session_id: sessionId }),
    connectSession: (sessionId) => record("connect_session", { session_id: sessionId }),
    disconnectSession: (sessionId) => record("disconnect_session", { session_id: sessionId }),
    quickConnect: (connectorType, options) => record("quick_connect", { connector_type: connectorType, ...options }),
    watchSessionEvents: (sessionId, options) => record("watch_session_events", { session_id: sessionId, ...options }),
    post: (path, body) => record("post", { path, body }),
    acquire: (workerId, options) => record("acquire", { worker_id: workerId, ...options }),
    release: (workerId, hijackId) => record("release", { worker_id: workerId, hijack_id: hijackId }),
    guiScreenshot: (workerId, hijackId) => record("gui_screenshot", { worker_id: workerId, hijack_id: hijackId }),
    guiClick: (workerId, hijackId, options) =>
      record("gui_click", { worker_id: workerId, hijack_id: hijackId, ...options }),
    guiType: (workerId, hijackId, options) =>
      record("gui_type", { worker_id: workerId, hijack_id: hijackId, ...options }),
    guiKey: (workerId, hijackId, options) =>
      record("gui_key", { worker_id: workerId, hijack_id: hijackId, ...options }),
    guiDrag: (workerId, hijackId, options) =>
      record("gui_drag", { worker_id: workerId, hijack_id: hijackId, ...options }),
  };
  return { client, calls };
}

describe("the nineteen tools that finish the surface", () => {
  it("registers what the reference registers", () => {
    expect([...SESSION_TOOL_NAMES]).toEqual(golden.session_tools);
    expect([...GUI_TOOL_NAMES]).toEqual(golden.gui_tools);
  });

  it.each(golden.cases)("$name", async (record) => {
    const { client, calls } = recordingClient(record.ok, record.data);
    const result = await callSessionTool(client, record.tool, record.args);

    // What was asked of the client is held even where the answer diverges:
    // a divergence in what a filter matches is not a licence to call
    // something else.
    expect(calls).toEqual(record.calls);
    if (record.diverges) {
      return;
    }
    expect(result).toEqual(record.result);
    expect(Object.keys(result)).toEqual(Object.keys(record.result));
  });

  it("refuses a tool nobody registered", async () => {
    const { client } = recordingClient(true, {});
    await expect(callSessionTool(client, "no_such_tool", {})).rejects.toThrow("unknown tool: no_such_tool");
  });
});

describe("starting a session, which is the widest thing a model can do", () => {
  it("vets the whole configuration before anything is spawned", async () => {
    // This tool can start a connector, so the refusal has to come before the
    // call rather than after it.
    for (const args of [
      { connector_type: "exec" },
      { connector_type: "telnet", host: "example.test", port: 0 },
      { connector_type: "telnet", host: "127.0.0.1", port: 23 },
      { connector_type: "websocket", url: "file:///etc/passwd" },
      { connector_type: "websocket", url: "ws://localhost:8080/x" },
    ]) {
      const { client, calls } = recordingClient(true, {});
      const result = await callSessionTool(client, "session_create", args);
      expect(result.success).toBe(false);
      expect(calls).toEqual([]);
    }
  });

  it("passes on only what it was given", async () => {
    // An absent field is absent rather than null: sending `username: null`
    // would be a request to connect as nobody.
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "session_create", { connector_type: "ssh", host: "example.test" });
    expect(calls[0]).toEqual({ method: "quick_connect", connector_type: "ssh", host: "example.test" });
  });

  it("leaves out a field the caller wrote as nothing", async () => {
    // The reference builds its call from the fields that are not `None`, so
    // `username: null` is a field nobody filled in rather than a request to
    // connect as nobody.
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "session_create", {
      connector_type: "ssh",
      host: "example.test",
      display_name: null,
      username: null,
      port: undefined,
    });
    expect(calls[0]).toEqual({ method: "quick_connect", connector_type: "ssh", host: "example.test" });
    expect(Object.keys(calls[0] as object)).toEqual(["method", "connector_type", "host"]);
  });

  it("carries every field the caller did give", async () => {
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "session_create", {
      connector_type: "ssh",
      display_name: "box",
      host: "example.test",
      port: 22,
      username: "ada",
      password: "hunter2", // pragma: allowlist secret
      input_mode: "open",
    });
    expect(calls[0]).toEqual({
      method: "quick_connect",
      connector_type: "ssh",
      display_name: "box",
      host: "example.test",
      port: 22,
      username: "ada",
      password: "hunter2", // pragma: allowlist secret
      input_mode: "open",
    });
  });
});

describe("watching and subscribing, which a model can ask too much of", () => {
  it("clamps how long a watch may run", async () => {
    // A model asking for an hour gets thirty seconds. The floor matters too:
    // asking for nothing would otherwise be a request that returns instantly
    // and can be repeated forever.
    for (const [asked, given] of [
      [600, WATCH_MAX_SECONDS * 1000],
      [0, 100],
      [-5, 100],
      [0.25, 250],
      [10, 10000],
    ] as const) {
      const { client, calls } = recordingClient(true, golden.events);
      await callSessionTool(client, "session_watch", { session_id: "s-1", timeout_s: asked });
      expect(calls[0]?.timeout_ms).toBe(given);
    }
  });

  it("clamps how many events a watch may collect", async () => {
    for (const [asked, given] of [
      [5000, WATCH_MAX_EVENTS],
      [0, 1],
      [-3, 1],
      [10, 10],
    ] as const) {
      const { client, calls } = recordingClient(true, golden.events);
      await callSessionTool(client, "session_watch", { session_id: "s-1", max_events: asked });
      expect(calls[0]?.max_events).toBe(given);
    }
  });

  it("gives a subscription a longer leash than a watch, but still one", async () => {
    for (const [asked, given] of [
      [9999, SUBSCRIBE_MAX_SECONDS * 1000],
      [0.1, 1000],
      [30, 30000],
    ] as const) {
      const { client, calls } = recordingClient(true, golden.events);
      await callSessionTool(client, "session_subscribe", { session_id: "s-1", duration_s: asked });
      expect(calls[0]?.timeout_ms).toBe(given);
    }
    const { client, calls } = recordingClient(true, golden.events);
    await callSessionTool(client, "session_subscribe", { session_id: "s-1", max_events: 100000 });
    expect(calls[0]?.max_events).toBe(SUBSCRIBE_MAX_EVENTS);
  });

  it("says whether the pattern actually fired, rather than that events arrived", async () => {
    // The fallback path — taken when there is no event bus — does not filter,
    // so events arriving proves nothing about the pattern.
    const { client } = recordingClient(true, golden.events);
    const fired = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern: "\\$ $" });
    expect(fired.matched_pattern).toBe(true);
    const quiet = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern: "never" });
    expect(quiet.matched_pattern).toBe(false);
  });

  it("says nothing fired when no pattern was asked for", async () => {
    const { client } = recordingClient(true, golden.events);
    const result = await callSessionTool(client, "session_subscribe", { session_id: "s-1" });
    expect(result.matched_pattern).toBe(false);
  });

  it("does not claim a pattern fired on an answer the server refused", async () => {
    const { client } = recordingClient(false, golden.events);
    const result = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern: "\\$ $" });
    expect(result.matched_pattern).toBe(false);
  });

  it("steps over anything in the events that is not shaped like one", async () => {
    // The list comes off the wire, so an entry that is a string, an event
    // with no payload, or a payload that is not a mapping must not stop the
    // scan before it reaches the events that are.
    const { client } = recordingClient(true, {
      events: ["a string", { type: "x" }, { data: null }, { data: "text" }, { data: { screen: "found it" } }],
    });
    const result = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern: "found" });
    expect(result.matched_pattern).toBe(true);
  });

  it("reads a screen that is not text as the text of it", async () => {
    // Faithful to the reference's `str(screen)`: a number on the screen is
    // matched as the digits it prints as.
    const { client } = recordingClient(true, { events: [{ data: { screen: 42 } }] });
    const result = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern: "42" });
    expect(result.matched_pattern).toBe(true);
  });

  it("takes a screen that is nothing as nothing, not as the word for it", async () => {
    // A recorded divergence. The reference renders a null screen with
    // `str(None)` and matches against the four characters `None`, so a
    // pattern like `^N` fires on a screen the terminal never showed. Here a
    // screen that is nothing matches nothing — the alternative is a filter
    // firing on an artifact of how the absence was written down.
    for (const events of [[{ data: { screen: null } }], [{ data: {} }], [{ data: { screen: undefined } }]]) {
      const { client } = recordingClient(true, { events });
      for (const pattern of ["None", "null", "undefined", "^$"]) {
        const result = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern });
        expect(result.matched_pattern).toBe(pattern === "^$");
      }
    }
  });

  it("steps over an event that is nothing at all", async () => {
    // `null` is an object to this runtime, so reading a field off it would
    // throw rather than skip — and one bad entry must not stop the scan
    // before the events that are real.
    const { client } = recordingClient(true, { events: [null, { data: { screen: "found it" } }] });
    const result = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern: "found" });
    expect(result.matched_pattern).toBe(true);
  });

  it("has nothing to scan when there are no events", async () => {
    const { client } = recordingClient(true, {});
    const result = await callSessionTool(client, "session_subscribe", { session_id: "s-1", pattern: "x" });
    expect(result.matched_pattern).toBe(false);
  });

  it("refuses a pattern that could hang before it asks for anything", async () => {
    for (const tool of ["session_watch", "session_subscribe"]) {
      const { client, calls } = recordingClient(true, golden.events);
      const result = await callSessionTool(client, tool, { session_id: "s-1", pattern: "(a+)+$" });
      expect(result.error).toBe("invalid_pattern");
      expect(calls).toEqual([]);
    }
  });

  it("checks the session id before the pattern", async () => {
    const { client } = recordingClient(true, golden.events);
    const result = await callSessionTool(client, "session_subscribe", { session_id: "a/b", pattern: "(a+)+" });
    expect(result.error).toBe("invalid_id");
  });
});

describe("the tools whose id lands in a URL", () => {
  it("refuses a group or session that is a path, without reaching", async () => {
    // These build the request path from the id, so a slash is a different
    // route entirely.
    for (const [tool, args] of [
      ["fanout_send", { group_id: "a/b", data: "x" }],
      ["session_annotate", { session_id: "../etc", label: "x" }],
    ] as const) {
      const { client, calls } = recordingClient(true, {});
      const result = await callSessionTool(client, tool, args as Record<string, unknown>);
      expect(result.error).toBe("invalid_id");
      expect(calls).toEqual([]);
    }
  });

  it("posts where the reference posts", async () => {
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "fanout_group_create", { session_ids: ["s-1"] });
    expect(calls[0]?.path).toBe("/api/fanout/groups");
    await callSessionTool(client, "fanout_send", { group_id: "g-1", data: "x" });
    expect(calls[1]?.path).toBe("/api/fanout/groups/g-1/send");
    await callSessionTool(client, "session_annotate", { session_id: "s-1", label: "x" });
    expect(calls[2]?.path).toBe("/api/sessions/s-1/annotate");
  });

  it("names a group and a mode when the caller did not", async () => {
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "fanout_group_create", { session_ids: ["s-1", "s-2"] });
    expect(calls[0]?.body).toEqual({ name: "fleet", worker_ids: ["s-1", "s-2"], mode: "parallel" });
  });

  it("makes a group of nobody rather than of something that is not a list", async () => {
    // The reference is typed `list[str]` and forwards whatever arrives, so a
    // string would reach the server as the members of a group. Nothing that
    // is not a list is a list of sessions, and a group of nobody broadcasts
    // to nobody.
    for (const session_ids of [undefined, null, "s-1", 7, { a: 1 }]) {
      const { client, calls } = recordingClient(true, {});
      await callSessionTool(client, "fanout_group_create", { session_ids });
      expect((calls[0]?.body as Record<string, unknown>).worker_ids).toEqual([]);
    }
  });

  it("marks a moment with the severity it was given, or none in particular", async () => {
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "session_annotate", { session_id: "s-1", label: "deploy" });
    expect(calls[0]?.body).toEqual({ label: "deploy", description: "", severity: "info" });
  });
});

describe("reaching a graphical console", () => {
  it("checks both ids before every one of them", async () => {
    for (const [tool, args] of [
      ["gui_hijack_begin", { worker_id: "a/b" }],
      ["gui_hijack_release", { worker_id: "w-1", hijack_id: "c/d" }],
      ["gui_screenshot", { worker_id: "a/b", hijack_id: "h-1" }],
      ["gui_click", { worker_id: "w-1", hijack_id: "c/d", x: 1, y: 1 }],
      ["gui_type", { worker_id: "a/b", hijack_id: "h-1", text: "x" }],
      ["gui_key", { worker_id: "a/b", hijack_id: "h-1", key_name: "Tab" }],
      ["gui_drag", { worker_id: "a/b", hijack_id: "h-1", start_x: 0, start_y: 0, end_x: 1, end_y: 1 }],
    ] as const) {
      const { client, calls } = recordingClient(true, {});
      const result = await callSessionTool(client, tool, args as Record<string, unknown>);
      expect(result.error).toBe("invalid_id");
      expect(calls).toEqual([]);
    }
  });

  it("takes a graphical lease the same way an ordinary one is taken", async () => {
    // The same endpoint: a graphical hijack is a hijack, and the lease it
    // holds is the one the input routes check.
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "gui_hijack_begin", { worker_id: "w-1" });
    expect(calls[0]).toEqual({ method: "acquire", worker_id: "w-1", owner: "operator", lease_s: 90 });
  });

  it("puts the pointer at the origin when nobody said where", async () => {
    // The reference requires the coordinates, so this only arrives from a
    // caller that bypassed the schema. The origin is a place on the screen;
    // guessing anywhere else would be inventing an instruction.
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "gui_click", { worker_id: "w-1", hijack_id: "h-1" });
    expect(calls[0]).toEqual({ method: "gui_click", worker_id: "w-1", hijack_id: "h-1", x: 0, y: 0, button: "left" });
    await callSessionTool(client, "gui_drag", { worker_id: "w-1", hijack_id: "h-1" });
    expect(calls[1]).toEqual({
      method: "gui_drag",
      worker_id: "w-1",
      hijack_id: "h-1",
      start_x: 0,
      start_y: 0,
      end_x: 0,
      end_y: 0,
    });
  });

  it("clicks the left button unless told otherwise", async () => {
    const { client, calls } = recordingClient(true, {});
    await callSessionTool(client, "gui_click", { worker_id: "w-1", hijack_id: "h-1", x: 3, y: 4 });
    expect(calls[0]).toEqual({
      method: "gui_click",
      worker_id: "w-1",
      hijack_id: "h-1",
      x: 3,
      y: 4,
      button: "left",
    });
  });
});
