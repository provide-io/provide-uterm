//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A session's definition, its runtime state, and the object they make.
 *
 * The whole object as a running server answers with it is held to
 * `serverhttp_golden` by `app.test.ts`. What is here is the filling-in — the
 * defaults a configured entry gets, and the deferral `recording_enabled: null`
 * stands for — plus the registry's own reads.
 */

import { describe, expect, it } from "vitest";
import { filterSessions, SessionRegistry } from "./session-registry.ts";
import {
  INITIAL_RUNTIME_STATE,
  recordingEnabled,
  sessionDefinitionFrom,
  sessionRuntimeStatus,
} from "./session-status.ts";

const CREATED = "2026-01-01T00:00:00.000Z";

describe("filling a configured entry out", () => {
  it("takes the defaults the reference's own model takes", () => {
    expect(sessionDefinitionFrom({ session_id: "one" }, CREATED)).toEqual({
      session_id: "one",
      display_name: "one",
      connector_type: "shell",
      connector_config: {},
      input_mode: "open",
      auto_start: true,
      tags: [],
      recording_enabled: null,
      created_at: CREATED,
      owner: null,
      visibility: "public",
    });
  });

  it("names a session after its id when nothing named it", () => {
    // Nothing a person reads is ever blank, which is the reference's own
    // before-validator rather than a nicety added here.
    expect(sessionDefinitionFrom({ session_id: "one", display_name: "" }, CREATED).display_name).toBe("one");
    expect(sessionDefinitionFrom({ session_id: "one", display_name: null }, CREATED).display_name).toBe("one");
  });

  it("trims the id, because it is a key and a path segment", () => {
    expect(sessionDefinitionFrom({ session_id: "  one  " }, CREATED).session_id).toBe("one");
  });

  it("keeps a created_at the configuration wrote down", () => {
    expect(sessionDefinitionFrom({ session_id: "one", created_at: "2020-05-05" }, CREATED).created_at).toBe(
      "2020-05-05",
    );
  });

  it("keeps everything a configuration did name", () => {
    expect(
      sessionDefinitionFrom(
        {
          session_id: "one",
          display_name: "One",
          connector_type: "telnet",
          connector_config: { host: "h" },
          input_mode: "hijack",
          auto_start: false,
          tags: ["a"],
          recording_enabled: true,
          owner: "someone",
          visibility: "private",
        },
        CREATED,
      ),
    ).toMatchObject({
      display_name: "One",
      connector_type: "telnet",
      connector_config: { host: "h" },
      input_mode: "hijack",
      auto_start: false,
      tags: ["a"],
      recording_enabled: true,
      owner: "someone",
      visibility: "private",
    });
  });

  it("has no id at all when the entry named none", () => {
    // Refusing it is `serverconfig`'s job; what this does is not invent one.
    expect(sessionDefinitionFrom({}, CREATED).session_id).toBe("");
  });
});

describe("whether a session records", () => {
  const definition = sessionDefinitionFrom({ session_id: "one" }, CREATED);

  it("defers to the deployment when the definition says nothing", () => {
    expect(recordingEnabled(definition, false)).toBe(false);
    expect(recordingEnabled(definition, true)).toBe(true);
  });

  it("is whatever the definition said when it said something", () => {
    // Including `false` against a deployment default of `true`: a session
    // that opted out must stay out.
    expect(recordingEnabled({ ...definition, recording_enabled: false }, true)).toBe(false);
    expect(recordingEnabled({ ...definition, recording_enabled: true }, false)).toBe(true);
  });
});

describe("the status object", () => {
  const definition = sessionDefinitionFrom({ session_id: "one", tags: ["a"] }, CREATED);

  it("reports recording available exactly when recording is enabled", () => {
    const status = sessionRuntimeStatus(definition, INITIAL_RUNTIME_STATE, true);
    expect(status.recording_enabled).toBe(true);
    expect(status.recording_available).toBe(true);
  });

  it("copies the tags rather than sharing them", () => {
    // A caller that sorted the answer would otherwise reorder the definition,
    // and the next request would come back different.
    const status = sessionRuntimeStatus(definition, INITIAL_RUNTIME_STATE, false);
    status.tags.push("b");
    expect(definition.tags).toEqual(["a"]);
  });

  it("starts stopped, unconnected, unstopped and without an error", () => {
    const status = sessionRuntimeStatus(definition, INITIAL_RUNTIME_STATE, false);
    expect(status.lifecycle_state).toBe("stopped");
    expect(status.connected).toBe(false);
    // Null rather than absent, so a client can tell "has not stopped" from
    // "this server does not say".
    expect(status.stopped_at).toBeNull();
    expect(status.last_error).toBeNull();
  });
});

describe("the registry", () => {
  const registry = new SessionRegistry(
    [sessionDefinitionFrom({ session_id: "one" }, CREATED), sessionDefinitionFrom({ session_id: "two" }, CREATED)],
    false,
  );

  it("counts what it holds", () => {
    expect(registry.size).toBe(2);
  });

  it("has nothing for an id no session has", () => {
    expect(registry.definition("three")).toBeUndefined();
    expect(registry.status("three")).toBeUndefined();
  });

  it("keeps configuration order", () => {
    expect(registry.statuses().map((one) => one.session_id)).toEqual(["one", "two"]);
  });

  it("moves a session to a new state", () => {
    const moving = new SessionRegistry([sessionDefinitionFrom({ session_id: "one" }, CREATED)], false);
    moving.setState("one", { lifecycle_state: "running", connected: true });
    expect(moving.status("one")).toMatchObject({ lifecycle_state: "running", connected: true });
  });

  it("ignores a state change for a session it does not hold", () => {
    // Not an error: a runtime reporting about a session that was deleted
    // under it is a race, not a fault.
    expect(() => registry.setState("three", { connected: true })).not.toThrow();
    expect(registry.status("one")?.connected).toBe(false);
  });
});

describe("narrowing a list", () => {
  it("does nothing at all when nothing was asked for", () => {
    const statuses = new SessionRegistry([sessionDefinitionFrom({ session_id: "one" }, CREATED)], false).statuses();
    expect(filterSessions(statuses)).toEqual(statuses);
  });

  it("keeps a session that matches any one of several tags", () => {
    const statuses = new SessionRegistry(
      [
        sessionDefinitionFrom({ session_id: "one", tags: ["a"] }, CREATED),
        sessionDefinitionFrom({ session_id: "two", tags: ["b"] }, CREATED),
      ],
      false,
    ).statuses();
    expect(filterSessions(statuses, { tag: ["a", "z"] }).map((one) => one.session_id)).toEqual(["one"]);
  });

  it("orders both ways, and pages from an offset", () => {
    const statuses = new SessionRegistry(
      [
        sessionDefinitionFrom({ session_id: "b" }, "2026-01-02T00:00:00.000Z"),
        sessionDefinitionFrom({ session_id: "a" }, "2026-01-01T00:00:00.000Z"),
        sessionDefinitionFrom({ session_id: "c" }, "2026-01-03T00:00:00.000Z"),
      ],
      false,
    ).statuses();
    const ids = (query: Parameters<typeof filterSessions>[1]) =>
      filterSessions(statuses, query).map((one) => one.session_id);
    expect(ids({ order: "asc" })).toEqual(["a", "b", "c"]);
    expect(ids({ order: "desc" })).toEqual(["c", "b", "a"]);
    expect(ids({ order: "asc", offset: 1, limit: 1 })).toEqual(["b"]);
  });

  it("treats an empty tag list as no tag filter", () => {
    const statuses = new SessionRegistry(
      [sessionDefinitionFrom({ session_id: "one", tags: ["a"] }, CREATED)],
      false,
    ).statuses();
    expect(filterSessions(statuses, { tag: [] })).toHaveLength(1);
  });
});
