//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type RegistrySocket, SocketRegistry, wsKey } from "./index.ts";

interface RegistryGolden {
  steps: Array<{
    name: string;
    worker: boolean;
    browsers: string[];
    raw: string[];
    hijack_owners: string[];
    resume_tokens: string[];
    forgotten: string[];
  }>;
  worker_replaced: boolean;
  worker_survives_browser_removal: boolean;
  key_is_stable: boolean;
  keys_differ: boolean;
}

const golden = loadGolden<RegistryGolden>("socketregistry_golden.json");

/** A flow controller that records what it was told to forget. */
class RecordingFlow {
  readonly forgotten: string[] = [];
  forget(wsId: string): void {
    this.forgotten.push(wsId);
  }
}

/** The session the corpus walks, replayed here. */
function replay(): Array<Record<string, unknown>> {
  const flow = new RecordingFlow();
  const registry = new SocketRegistry(flow);
  const sockets: Record<string, RegistrySocket> = {
    worker: {},
    "browser-a": {},
    "browser-b": {},
    "raw-a": {},
  };
  const named = (key: string): string =>
    Object.entries(sockets).find(([, socket]) => wsKey(socket) === key)?.[0] ?? key;
  const steps: Array<Record<string, unknown>> = [];
  const record = (name: string): void => {
    steps.push({
      name,
      worker: registry.worker !== undefined,
      browsers: [...registry.browsers.keys()].map(named).sort(),
      raw: [...registry.raw.keys()].map(named).sort(),
      hijack_owners: [...registry.hijackOwners.keys()].map(named).sort(),
      resume_tokens: [...registry.resumeTokens.keys()].map(named).sort(),
      forgotten: flow.forgotten.map(named),
    });
  };

  for (const [name, socket] of Object.entries(sockets)) {
    registry.register(socket, name === "worker" ? "worker" : name.startsWith("raw") ? "raw" : "browser");
    record(`registered ${name}`);
  }
  registry.hijackOwners.set(wsKey(sockets["browser-a"] as RegistrySocket), "u1");
  registry.resumeTokens.set(wsKey(sockets["browser-a"] as RegistrySocket), "t1");
  record("browser-a owns the hijack");

  for (const name of ["browser-a", "raw-a", "worker", "browser-b"]) {
    registry.remove(sockets[name] as RegistrySocket);
    record(name === "worker" ? "the worker leaves" : `${name} leaves`);
  }
  return steps;
}

describe("the session's socket registry", () => {
  it("walks a session filling up and emptying again", () => {
    expect(replay()).toEqual(golden.steps);
  });

  it("keeps one worker and many of everything else", () => {
    // A session has exactly one worker at a time, so a second replaces the
    // first rather than accumulating — output going to a socket nobody reads.
    const registry = new SocketRegistry(new RecordingFlow());
    const first: RegistrySocket = {};
    const second: RegistrySocket = {};
    registry.register(first, "worker");
    registry.register(second, "worker");
    expect(registry.worker).toBe(second);
    expect(golden.worker_replaced).toBe(true);

    registry.register({}, "browser");
    registry.register({}, "browser");
    registry.register({}, "raw");
    expect(registry.browsers.size).toBe(2);
    expect(registry.raw.size).toBe(1);
  });

  it("treats anything it does not recognise as a browser", () => {
    // The overwhelming majority are, and the safest thing to be mistaken for.
    const registry = new SocketRegistry(new RecordingFlow());
    registry.register({}, "nonsense");
    registry.register({}, "");
    expect(registry.browsers.size).toBe(2);
    expect(registry.worker).toBeUndefined();
  });

  it("lets go of everything keyed by a connection", () => {
    // Anything left behind is state for a connection that no longer exists.
    const flow = new RecordingFlow();
    const registry = new SocketRegistry(flow);
    const socket: RegistrySocket = {};
    registry.register(socket, "browser");
    const key = wsKey(socket);
    registry.hijackOwners.set(key, "u1");
    registry.resumeTokens.set(key, "t1");

    registry.remove(socket);
    expect(registry.browsers.has(key)).toBe(false);
    expect(registry.hijackOwners.has(key)).toBe(false);
    expect(registry.resumeTokens.has(key)).toBe(false);
    expect(flow.forgotten).toEqual([key]);
  });

  it("does not detach the worker when a browser leaves", () => {
    // By identity rather than by id, which is what makes this safe.
    const registry = new SocketRegistry(new RecordingFlow());
    const worker: RegistrySocket = {};
    const browser: RegistrySocket = {};
    registry.register(worker, "worker");
    registry.register(browser, "browser");
    registry.remove(browser);
    expect(registry.worker).toBe(worker);
    expect(golden.worker_survives_browser_removal).toBe(true);
  });

  it("forgets a connection it never held", () => {
    // A socket removed twice, or one that failed before registering, still
    // has to clear whatever the flow controller learned about it.
    const flow = new RecordingFlow();
    const registry = new SocketRegistry(flow);
    const socket: RegistrySocket = {};
    registry.remove(socket);
    registry.remove(socket);
    expect(flow.forgotten).toHaveLength(2);
    expect(registry.browsers.size).toBe(0);
  });
});

describe("who else is connected", () => {
  it("names the browsers and nothing else", () => {
    // A worker and a raw connection are not peers a browser is told about.
    const registry = new SocketRegistry(new RecordingFlow());
    const browser: RegistrySocket = {};
    registry.register(browser, "browser");
    registry.register({}, "worker");
    registry.register({}, "raw");
    expect(registry.presenceIds()).toEqual([wsKey(browser)]);
  });

  it("leaves out the browser being told", () => {
    // It is already registered by the time the runtime reports the
    // connection open, so a joining browser would otherwise be told about
    // itself.
    const registry = new SocketRegistry(new RecordingFlow());
    const joining: RegistrySocket = {};
    const existing: RegistrySocket = {};
    registry.register(existing, "browser");
    registry.register(joining, "browser");
    expect(registry.presenceIds({ exclude: joining })).toEqual([wsKey(existing)]);
  });

  it("names everybody when told to exclude nobody", () => {
    // Which is the case before the socket is registered at all.
    const registry = new SocketRegistry(new RecordingFlow());
    const first: RegistrySocket = {};
    const second: RegistrySocket = {};
    registry.register(first, "browser");
    registry.register(second, "browser");
    expect(registry.presenceIds().sort()).toEqual([wsKey(first), wsKey(second)].sort());
  });

  it("names nobody on an empty session", () => {
    expect(new SocketRegistry(new RecordingFlow()).presenceIds()).toEqual([]);
  });

  it("prefers the runtime's own list", () => {
    // Which is what survives hibernation: an object resumed with its sockets
    // still open has an empty registry and a full runtime, and a list built
    // from the registry alone would tell every browser it was alone.
    const registry = new SocketRegistry(new RecordingFlow());
    const resumed: RegistrySocket = {};
    const roles = new Map<RegistrySocket, string>([[resumed, "browser"]]);
    expect(registry.browsers.size).toBe(0);
    expect(
      registry.presenceIds({ liveSockets: () => [resumed], roleOf: (socket) => roles.get(socket) ?? "browser" }),
    ).toEqual([wsKey(resumed)]);
  });

  it("filters the runtime's list by role", () => {
    // It carries every socket, not only the browsers.
    const registry = new SocketRegistry(new RecordingFlow());
    const browser: RegistrySocket = {};
    const worker: RegistrySocket = {};
    const roles = new Map<RegistrySocket, string>([
      [browser, "browser"],
      [worker, "worker"],
    ]);
    expect(
      registry.presenceIds({
        liveSockets: () => [browser, worker],
        roleOf: (socket) => roles.get(socket) ?? "browser",
      }),
    ).toEqual([wsKey(browser)]);
  });

  it("falls back to the registry when the runtime reports nothing", () => {
    const registry = new SocketRegistry(new RecordingFlow());
    const browser: RegistrySocket = {};
    registry.register(browser, "browser");
    expect(registry.presenceIds({ liveSockets: () => [] })).toEqual([wsKey(browser)]);
  });

  it("excludes the joining browser from the runtime's list too", () => {
    const registry = new SocketRegistry(new RecordingFlow());
    const joining: RegistrySocket = {};
    const existing: RegistrySocket = {};
    expect(registry.presenceIds({ exclude: joining, liveSockets: () => [existing, joining] })).toEqual([
      wsKey(existing),
    ]);
  });
});

describe("a connection's identity", () => {
  it("is stable for one socket", () => {
    // It survives the runtime handing the socket back after eviction, which
    // nothing derived from the connection itself would.
    const socket: RegistrySocket = {};
    expect(wsKey(socket)).toBe(wsKey(socket));
    expect(golden.key_is_stable).toBe(true);
  });

  it("differs between sockets", () => {
    expect(wsKey({})).not.toBe(wsKey({}));
    expect(golden.keys_differ).toBe(true);
  });

  it("is stamped on the socket", () => {
    const socket: RegistrySocket = {};
    const key = wsKey(socket);
    expect(socket._ut_ws_key).toBe(key);
  });

  it("keeps a key the socket already carries", () => {
    // A socket resumed after eviction arrives with its key already on it.
    expect(wsKey({ _ut_ws_key: "existing" })).toBe("existing");
  });

  it("does not keep an empty key", () => {
    // Which would make every such socket the same connection.
    expect(wsKey({ _ut_ws_key: "" })).not.toBe("");
  });

  it("still answers for a socket it cannot stamp", () => {
    // Worse than remembering it, and better than failing to register it.
    const frozen = Object.freeze({}) as RegistrySocket;
    expect(wsKey(frozen)).not.toBe("");
    expect(wsKey(frozen)).not.toBe(wsKey(frozen));
  });

  it("distinguishes two sockets made in the same instant", () => {
    // A timestamp alone would collide; the random half is what separates two
    // browsers connecting together.
    const clock = () => 1_700_000_000_000_000_000n;
    expect(wsKey({}, clock)).not.toBe(wsKey({}, clock));
  });
});
