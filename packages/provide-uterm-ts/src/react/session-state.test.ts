//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { canType, INITIAL_STATE, SCREEN_CAP, type SessionState, sessionReducer } from "./index.ts";

/** The state a run of events leaves behind, starting from nothing.
 *
 * Named `stateAfter` rather than `after`: `after` is a test-hook name in
 * Mocha and in Node's runner, so a helper called that reads as a hook —
 * to a person skimming the file, and to biome, which flagged all 39 call
 * sites as duplicate hooks.
 */
function stateAfter(
  events: Array<Parameters<typeof sessionReducer>[1]>,
  viewerId?: string,
  from: SessionState = INITIAL_STATE,
): SessionState {
  return events.reduce((state, event) => sessionReducer(state, event, viewerId), from);
}

/** A control frame arriving. */
function control(frame: Record<string, unknown>): Parameters<typeof sessionReducer>[1] {
  return { kind: "control", frame };
}

describe("what a viewer starts with", () => {
  it("assumes nothing before the server has said anything", () => {
    // Every field a view renders has to have an answer before the first frame,
    // and "connecting" is the only honest one.
    expect(INITIAL_STATE).toEqual({
      status: "connecting",
      screen: "",
      sessionId: undefined,
      hijackHolder: undefined,
      isHolder: false,
      inputMode: "open",
      participants: [],
      approvals: [],
      error: undefined,
      reconnects: 0,
    });
  });

  it("does not let anybody type before the connection is up", () => {
    expect(canType(INITIAL_STATE)).toBe(false);
  });
});

describe("the connection coming and going", () => {
  it("opens", () => {
    expect(stateAfter([{ kind: "opened" }]).status).toBe("open");
  });

  it("counts a reconnection, not a first connection", () => {
    expect(stateAfter([{ kind: "opened" }]).reconnects).toBe(0);
    expect(stateAfter([{ kind: "opened" }, { kind: "closed" }, { kind: "opened" }]).reconnects).toBe(1);
    expect(
      stateAfter([{ kind: "opened" }, { kind: "closed" }, { kind: "opened" }, { kind: "closed" }, { kind: "opened" }])
        .reconnects,
    ).toBe(2);
  });

  it("keeps the screen across a reconnection", () => {
    // The session is the same one; clearing would lose what somebody was
    // reading at the moment the network blinked.
    const state = stateAfter([
      { kind: "opened" },
      { kind: "data", data: "important output" },
      { kind: "closed" },
      { kind: "opened" },
    ]);
    expect(state.screen).toBe("important output");
  });

  it("drops everything that only the server could keep true", () => {
    // A presence list nobody is updating is a list of people who may have
    // left, and a hijack holder nobody is updating is a lock that may be gone.
    const state = stateAfter(
      [
        { kind: "opened" },
        control({ type: "hijack_state", holder: "ada" }),
        control({ type: "presence_sync", participants: [{ id: "v1", name: "Ada" }] }),
        control({ type: "approval_pending", approval_id: "a1", subject: "Ada" }),
        { kind: "closed" },
      ],
      "ada",
    );
    expect(state).toMatchObject({
      status: "closed",
      hijackHolder: undefined,
      isHolder: false,
      participants: [],
      approvals: [],
    });
  });

  it("clears an error when the connection comes back", () => {
    const state = stateAfter([{ kind: "opened" }, control({ type: "error", message: "gone" }), { kind: "opened" }]);
    expect(state.error).toBeUndefined();
  });
});

describe("what the terminal has shown", () => {
  it("appends in the order it arrived", () => {
    expect(
      stateAfter([
        { kind: "data", data: "one" },
        { kind: "data", data: "two" },
      ]).screen,
    ).toBe("onetwo");
  });

  it("keeps the newest when more arrives than it shows", () => {
    const state = stateAfter([
      { kind: "data", data: "x".repeat(SCREEN_CAP) },
      { kind: "data", data: "y".repeat(100) },
    ]);
    expect(state.screen).toHaveLength(SCREEN_CAP);
    expect(state.screen.endsWith("y".repeat(100))).toBe(true);
  });

  it("caps at the same size the connector does", () => {
    // A view that kept more than the worker sends would still only ever be
    // shown that much.
    expect(SCREEN_CAP).toBe(32768);
  });

  it("forgets on request without touching anything else", () => {
    const state = stateAfter([{ kind: "opened" }, { kind: "data", data: "gone" }, { kind: "cleared" }]);
    expect(state.screen).toBe("");
    expect(state.status).toBe("open");
  });
});

describe("who holds the session", () => {
  it("takes the holder from the server and compares it with this viewer", () => {
    // Never from what this client asked for: two clients that each believed
    // their own request would both type into one shell.
    expect(stateAfter([control({ type: "hijack_state", holder: "ada" })], "ada")).toMatchObject({
      hijackHolder: "ada",
      isHolder: true,
    });
    expect(stateAfter([control({ type: "hijack_state", holder: "ada" })], "bob")).toMatchObject({
      hijackHolder: "ada",
      isHolder: false,
    });
  });

  it("is nobody's when the server says so", () => {
    const state = stateAfter(
      [control({ type: "hijack_state", holder: "ada" }), control({ type: "hijack_state" })],
      "ada",
    );
    expect(state).toMatchObject({ hijackHolder: undefined, isHolder: false });
  });

  it("does not make an anonymous viewer the holder", () => {
    // Without an identity there is nothing to compare, and guessing yes would
    // hand control to every viewer at once.
    expect(stateAfter([control({ type: "hijack_state", holder: "ada" })], undefined).isHolder).toBe(false);
  });

  it("reads the holder under either name the server uses", () => {
    expect(stateAfter([control({ type: "hijack_state", holder_id: "ada" })], "ada").isHolder).toBe(true);
  });
});

describe("who else is here", () => {
  it("takes the whole list from a sync", () => {
    const state = stateAfter([
      control({
        type: "presence_sync",
        participants: [
          { id: "v1", name: "Ada", role: "operator", color: "#f00" },
          { id: "v2", display_name: "Bob" },
        ],
      }),
    ]);
    expect(state.participants).toEqual([
      { id: "v1", name: "Ada", role: "operator", colour: "#f00" },
      { id: "v2", name: "Bob", role: "viewer", colour: undefined },
    ]);
  });

  it("names somebody by their id when they have no name", () => {
    // Better than an empty row, which reads as a bug.
    expect(stateAfter([control({ type: "presence_sync", participants: [{ id: "v1" }] })]).participants[0]?.name).toBe(
      "v1",
    );
  });

  it("leaves out somebody with no id at all", () => {
    // They cannot be told apart from anybody else, and would reappear as a new
    // row on every frame.
    expect(
      stateAfter([control({ type: "presence_sync", participants: [{ name: "nobody" }, { id: "v1" }] })]).participants,
    ).toHaveLength(1);
  });

  it("survives a presence frame that is not a list", () => {
    for (const participants of [undefined, null, "nobody", 42, {}]) {
      expect(stateAfter([control({ type: "presence_sync", participants })]).participants).toEqual([]);
    }
  });

  it("survives entries that are not objects", () => {
    expect(
      stateAfter([control({ type: "presence_sync", participants: [null, "x", 42, { id: "v1" }] })]).participants,
    ).toHaveLength(1);
  });

  it("adds somebody who arrives", () => {
    const state = stateAfter([
      control({ type: "presence_sync", participants: [{ id: "v1", name: "Ada" }] }),
      control({ type: "presence_update", participant: { id: "v2", name: "Bob" } }),
    ]);
    expect(state.participants.map((participant) => participant.id)).toEqual(["v1", "v2"]);
  });

  it("updates somebody in place rather than moving them", () => {
    // A list people are reading should not reshuffle because somebody's role
    // changed.
    const state = stateAfter([
      control({
        type: "presence_sync",
        participants: [
          { id: "v1", name: "Ada" },
          { id: "v2", name: "Bob" },
        ],
      }),
      control({ type: "presence_update", participant: { id: "v1", name: "Ada", role: "operator" } }),
    ]);
    expect(state.participants.map((participant) => participant.id)).toEqual(["v1", "v2"]);
    expect(state.participants[0]?.role).toBe("operator");
  });

  it("takes an update given without a wrapper", () => {
    expect(stateAfter([control({ type: "presence_update", id: "v1", name: "Ada" })]).participants).toHaveLength(1);
  });

  it("ignores an update naming nobody", () => {
    expect(stateAfter([control({ type: "presence_update", participant: { name: "Ada" } })]).participants).toEqual([]);
  });

  it("removes somebody who leaves", () => {
    const state = stateAfter([
      control({ type: "presence_sync", participants: [{ id: "v1" }, { id: "v2" }] }),
      control({ type: "presence_leave", viewer_id: "v1" }),
    ]);
    expect(state.participants.map((participant) => participant.id)).toEqual(["v2"]);
  });

  it("ignores a departure naming nobody", () => {
    const state = stateAfter([
      control({ type: "presence_sync", participants: [{ id: "v1" }] }),
      control({ type: "presence_leave" }),
    ]);
    expect(state.participants).toHaveLength(1);
  });
});

describe("things waiting for a decision", () => {
  it("holds them in the order they arrived", () => {
    const state = stateAfter([
      control({ type: "approval_pending", approval_id: "a1", subject: "Ada", reason: "to fix the build" }),
      control({ type: "approval_pending", approval_id: "a2", subject: "Bob" }),
    ]);
    expect(state.approvals).toEqual([
      { id: "a1", subject: "Ada", reason: "to fix the build" },
      { id: "a2", subject: "Bob", reason: undefined },
    ]);
  });

  it("does not show a retried request twice", () => {
    // A server repeating itself is not a second person asking.
    const state = stateAfter([
      control({ type: "approval_pending", approval_id: "a1", subject: "Ada" }),
      control({ type: "approval_pending", approval_id: "a1", subject: "Ada" }),
    ]);
    expect(state.approvals).toHaveLength(1);
  });

  it("ignores a request with nothing to answer about", () => {
    expect(stateAfter([control({ type: "approval_pending", subject: "Ada" })]).approvals).toEqual([]);
  });

  it("names somebody even when the server did not", () => {
    expect(stateAfter([control({ type: "approval_pending", id: "a1" })]).approvals[0]?.subject).toBe("somebody");
  });

  it("removes one that has been decided", () => {
    const state = stateAfter([
      control({ type: "approval_pending", approval_id: "a1", subject: "Ada" }),
      control({ type: "approval_pending", approval_id: "a2", subject: "Bob" }),
      control({ type: "approval_resolved", approval_id: "a1" }),
    ]);
    expect(state.approvals.map((approval) => approval.id)).toEqual(["a2"]);
  });

  it("ignores a resolution naming nothing", () => {
    const state = stateAfter([
      control({ type: "approval_pending", approval_id: "a1", subject: "Ada" }),
      control({ type: "approval_resolved" }),
    ]);
    expect(state.approvals).toHaveLength(1);
  });
});

describe("the rest of what a server says", () => {
  it("takes the session's name from the greeting", () => {
    expect(stateAfter([control({ type: "hello", session_id: "sess-1" })]).sessionId).toBe("sess-1");
  });

  it("keeps the name it had when a later greeting omits it", () => {
    const state = stateAfter([control({ type: "hello", session_id: "sess-1" }), control({ type: "hello" })]);
    expect(state.sessionId).toBe("sess-1");
  });

  it("follows the input mode from either frame that carries it", () => {
    expect(stateAfter([control({ type: "worker_hello", input_mode: "hijack" })]).inputMode).toBe("hijack");
    expect(stateAfter([control({ type: "input_mode_changed", input_mode: "open" })]).inputMode).toBe("open");
  });

  it("keeps the mode it had when a frame omits it", () => {
    const state = stateAfter([
      control({ type: "worker_hello", input_mode: "hijack" }),
      control({ type: "input_mode_changed" }),
    ]);
    expect(state.inputMode).toBe("hijack");
  });

  it("shows an error under either name it arrives with", () => {
    expect(stateAfter([control({ type: "error", message: "denied" })]).error).toBe("denied");
    expect(stateAfter([control({ type: "error", error: "denied" })]).error).toBe("denied");
    expect(stateAfter([control({ type: "error" })]).error).toBe("unknown error");
  });

  it("ignores a frame it has never heard of", () => {
    // A newer server may say more than an older viewer understands, and losing
    // the session over it would be the worse failure.
    const before = stateAfter([{ kind: "opened" }, { kind: "data", data: "hi" }]);
    expect(stateAfter([control({ type: "quantum_entangle", spin: "up" })], undefined, before)).toEqual(before);
    expect(stateAfter([control({})], undefined, before)).toEqual(before);
  });

  it("ignores a field of the wrong type rather than showing it", () => {
    expect(stateAfter([control({ type: "hello", session_id: 42 })]).sessionId).toBeUndefined();
    expect(stateAfter([control({ type: "hello", session_id: "" })]).sessionId).toBeUndefined();
  });
});

describe("whether this viewer may type", () => {
  const open = stateAfter([{ kind: "opened" }]);

  it("needs the connection to be up", () => {
    expect(canType(open)).toBe(true);
    expect(canType({ ...open, status: "closed" })).toBe(false);
    expect(canType({ ...open, status: "connecting" })).toBe(false);
  });

  it("needs the lease when the worker is locked", () => {
    // In hijack mode nobody types without holding it — not even when the
    // holder is unknown, which is the state a lost frame leaves behind.
    expect(canType({ ...open, inputMode: "hijack", isHolder: true })).toBe(true);
    expect(canType({ ...open, inputMode: "hijack", isHolder: false })).toBe(false);
    expect(canType({ ...open, inputMode: "hijack", isHolder: false, hijackHolder: undefined })).toBe(false);
  });

  it("lets an open session through unless somebody else holds it", () => {
    expect(canType({ ...open, hijackHolder: undefined })).toBe(true);
    expect(canType({ ...open, hijackHolder: "ada", isHolder: true })).toBe(true);
    expect(canType({ ...open, hijackHolder: "ada", isHolder: false })).toBe(false);
  });
});
