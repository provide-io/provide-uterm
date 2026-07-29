//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Bringing sessions up, and what the wire says while it happens.
 *
 * The expected states are not written here: they come from
 * `sessionruntime_golden.json`, which `gen_sessionruntime_golden.py` records by
 * driving the reference's own `HostedSessionRuntime` through each transition.
 * The one place this port deliberately reports something else is asserted
 * against the golden value it differs from, so the difference is a decision on
 * the record rather than a drift nobody noticed.
 */

import { describe, expect, it } from "vitest";
import { SESSION_LIFECYCLES } from "../bridge/index.ts";
import type { SessionConnector, WorkerMessage } from "../connectors/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { SessionRegistry } from "./session-registry.ts";
import { SessionRuntimes } from "./session-runtime.ts";
import { type SessionLifecycle, sessionDefinitionFrom } from "./session-status.ts";

/** The runtime-owned fields of a status, as the reference reports them. */
interface Observed {
  lifecycle_state: string;
  connected: boolean;
  last_error: string | null;
  stopped_at_set: boolean;
}

interface RuntimeGolden {
  lifecycles: string[];
  initial: Observed;
  starting: Observed;
  started_twice: Observed;
  stopped: Observed;
  unsupported_connector: Observed;
}

const golden = loadGolden<RuntimeGolden>("sessionruntime_golden.json");

const CREATED = "2026-01-01T00:00:00.000Z";

/** How a fake connector should behave when it is started. */
interface FakeOptions {
  /** Refuse to start, with this message. */
  failWith?: string;
  /** Do not finish starting until this resolves. */
  gate?: Promise<void>;
}

/** A connector that records what was asked of it. */
class FakeConnector implements SessionConnector {
  started = 0;
  stopped = 0;
  readonly #options: FakeOptions;

  constructor(options: FakeOptions = {}) {
    this.#options = options;
  }

  async start(): Promise<void> {
    if (this.#options.gate !== undefined) {
      await this.#options.gate;
    }
    if (this.#options.failWith !== undefined) {
      throw new Error(this.#options.failWith);
    }
    this.started += 1;
  }

  async stop(): Promise<void> {
    this.stopped += 1;
  }

  isConnected(): boolean {
    return this.started > this.stopped;
  }

  async pollMessages(): Promise<WorkerMessage[]> {
    return [];
  }

  async handleInput(): Promise<WorkerMessage[]> {
    return [];
  }

  async handleControl(): Promise<WorkerMessage[]> {
    return [];
  }

  async getSnapshot(): Promise<WorkerMessage> {
    return {};
  }

  async getAnalysis(): Promise<string> {
    return "";
  }

  async setMode(): Promise<WorkerMessage[]> {
    return [];
  }

  async clear(): Promise<WorkerMessage[]> {
    return [];
  }
}

/** The runtime-owned fields of one session, in the golden's own shape. */
function observe(registry: SessionRegistry, sessionId: string): Observed {
  const status = registry.status(sessionId);
  if (status === undefined) {
    throw new Error(`no session ${sessionId}`);
  }
  return {
    lifecycle_state: status.lifecycle_state,
    connected: status.connected,
    last_error: status.last_error,
    stopped_at_set: status.stopped_at !== null,
  };
}

/** A registry over the given entries, with the defaults filled in. */
function registryOf(...entries: Readonly<Record<string, unknown>>[]): SessionRegistry {
  return new SessionRegistry(
    entries.map((entry) => sessionDefinitionFrom(entry, CREATED)),
    false,
  );
}

describe("bringing one session up", () => {
  it("starts stopped, exactly as the reference does", () => {
    expect(observe(registryOf({ session_id: "one" }), "one")).toEqual(golden.initial);
  });

  it("runs the session's own connector, and says running once it is up", async () => {
    const registry = registryOf({ session_id: "one", connector_type: "shell" });
    const connector = new FakeConnector();
    const runtimes = new SessionRuntimes(registry, { build: () => connector });

    await runtimes.start("one");

    expect(connector.started).toBe(1);
    // `running`, and `connected` still false: the connector is up, but this
    // port binds no route a client could reach it through, and those are two
    // questions the wire asks with two fields.
    expect(observe(registry, "one")).toEqual({
      lifecycle_state: "running",
      connected: false,
      last_error: null,
      stopped_at_set: false,
    });
  });

  it("builds the connector the definition named, with the configuration it named", async () => {
    const registry = registryOf({
      session_id: "one",
      display_name: "One",
      connector_type: "telnet",
      connector_config: { host: "h" },
    });
    const seen: unknown[][] = [];
    const runtimes = new SessionRuntimes(registry, {
      build: (...args) => {
        seen.push(args);
        return new FakeConnector();
      },
    });

    await runtimes.start("one");

    expect(seen).toEqual([["one", "One", "telnet", { host: "h" }]]);
  });

  it("hands the connector a copy of the configuration, not the definition's own", async () => {
    // A connector that normalised its settings in place would otherwise
    // rewrite the definition, and the next request would answer differently.
    const registry = registryOf({ session_id: "one", connector_config: { host: "h" } });
    const runtimes = new SessionRuntimes(registry, {
      build: (_id, _name, _type, config) => {
        config.host = "rewritten";
        return new FakeConnector();
      },
    });

    await runtimes.start("one");

    expect(registry.definition("one")?.connector_config).toEqual({ host: "h" });
  });

  it("clears a previous stop and a previous error on the way up", async () => {
    // The reference's `start` sets `starting` and clears `stopped_at` and
    // `last_error` with it: what failed last time is not what is happening now.
    const registry = registryOf({ session_id: "one" });
    registry.setState("one", { lifecycle_state: "error", last_error: "old", stopped_at: 1 });
    const runtimes = new SessionRuntimes(registry, { build: () => new FakeConnector() });

    await runtimes.start("one");

    expect(observe(registry, "one")).toEqual({
      lifecycle_state: "running",
      connected: false,
      last_error: null,
      stopped_at_set: false,
    });
  });

  it("says starting while the connector is still coming up", async () => {
    // The state a request arriving mid-start is answered with — the
    // reference's own `starting`, not a gap between stopped and running.
    const registry = registryOf({ session_id: "one" });
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const runtimes = new SessionRuntimes(registry, { build: () => new FakeConnector({ gate }) });

    const starting = runtimes.start("one");
    expect(observe(registry, "one")).toEqual(golden.starting);

    release();
    await starting;
    expect(observe(registry, "one").lifecycle_state).toBe("running");
  });

  it("does nothing at all for a session it does not hold", async () => {
    // A start for a session that was deleted under the caller is a race, not a
    // fault — the same tolerance the registry's own `setState` has.
    const registry = registryOf({ session_id: "one" });
    let built = 0;
    const runtimes = new SessionRuntimes(registry, {
      build: () => {
        built += 1;
        return new FakeConnector();
      },
    });

    await expect(runtimes.start("nobody")).resolves.toBeUndefined();
    expect(built).toBe(0);
  });

  it("leaves a session that is already up alone", async () => {
    // The reference returns early on a task that is still running, so a second
    // start neither rebuilds the connector nor rewinds the state.
    const registry = registryOf({ session_id: "one" });
    let built = 0;
    const runtimes = new SessionRuntimes(registry, {
      build: () => {
        built += 1;
        return new FakeConnector();
      },
    });

    await runtimes.start("one");
    await runtimes.start("one");

    expect(built).toBe(1);
    // The reference records `starting` here only because its own start never
    // completes without a hub to connect to; what both agree on is that the
    // second start moved nothing.
    expect(golden.started_twice).toEqual(golden.starting);
    expect(observe(registry, "one")).toEqual({
      lifecycle_state: "running",
      connected: false,
      last_error: null,
      stopped_at_set: false,
    });
  });
});

describe("a session that will not come up", () => {
  it("comes to rest where the reference does, saying what failed", async () => {
    const registry = registryOf({ session_id: "one", connector_type: "no-such-connector" });
    const runtimes = new SessionRuntimes(registry);

    await runtimes.start("one");

    // Every runtime-owned field, against the reference's own recording of the
    // same refusal: `stopped`, the message word for word, and `stopped_at`
    // written. Nothing here is this port's own answer.
    expect(observe(registry, "one")).toEqual(golden.unsupported_connector);
  });

  it("is told apart from a session nobody started by last_error, not by the state", async () => {
    // The two reach the same `stopped`, which is the reference's behaviour and
    // the reason `last_error` has to carry the difference. A test that let
    // these two states diverge would be re-introducing the bug.
    const never = registryOf({ session_id: "one" });
    const failed = registryOf({ session_id: "one", connector_type: "no-such-connector" });
    await new SessionRuntimes(failed).start("one");

    expect(observe(never, "one").lifecycle_state).toBe(observe(failed, "one").lifecycle_state);
    expect(observe(never, "one").last_error).toBeNull();
    expect(observe(failed, "one").last_error).not.toBeNull();
  });

  it("says so for a failure that was not an Error at all", async () => {
    const registry = registryOf({ session_id: "one" });
    const runtimes = new SessionRuntimes(registry, {
      build: () => {
        throw "a string nobody wrapped";
      },
    });

    await runtimes.start("one");

    expect(observe(registry, "one").last_error).toBe("a string nobody wrapped");
  });

  it("says so when the connector was built but refused to start", async () => {
    const registry = registryOf({ session_id: "one" });
    const runtimes = new SessionRuntimes(registry, {
      build: () => new FakeConnector({ failWith: "upstream refused" }),
    });

    await runtimes.start("one");

    expect(observe(registry, "one")).toEqual({
      lifecycle_state: "stopped",
      connected: false,
      last_error: "upstream refused",
      stopped_at_set: true,
    });
  });

  it("does not hold on to a connector that never started", async () => {
    const registry = registryOf({ session_id: "one" });
    const connector = new FakeConnector({ failWith: "upstream refused" });
    const runtimes = new SessionRuntimes(registry, { build: () => connector });

    await runtimes.start("one");

    expect(runtimes.connector("one")).toBeUndefined();
    await runtimes.stopAll();
    expect(connector.stopped).toBe(0);
  });
});

describe("honouring auto_start", () => {
  it("brings up every session the configuration flagged, and only those", async () => {
    const registry = registryOf(
      { session_id: "on" },
      { session_id: "off", auto_start: false },
      { session_id: "also-on", auto_start: true },
    );
    const runtimes = new SessionRuntimes(registry, { build: () => new FakeConnector() });

    await runtimes.startAutoStart();

    expect(registry.statuses().map((one) => [one.session_id, one.lifecycle_state])).toEqual([
      ["on", "running"],
      ["off", "stopped"],
      ["also-on", "running"],
    ]);
  });

  it("carries on past one that will not come up", async () => {
    // One session nobody can start must not cost every other session its boot.
    const registry = registryOf({ session_id: "first", connector_type: "no-such-connector" }, { session_id: "second" });
    const runtimes = new SessionRuntimes(registry, {
      build: (_id, _name, type) => {
        if (type === "no-such-connector") {
          throw new Error("unsupported connector_type");
        }
        return new FakeConnector();
      },
    });

    await runtimes.startAutoStart();

    // The one that failed says so in `last_error`; the one after it still came
    // up, which is the whole point of not aborting the batch.
    expect(observe(registry, "first")).toMatchObject({
      lifecycle_state: "stopped",
      last_error: "unsupported connector_type",
    });
    expect(observe(registry, "second").lifecycle_state).toBe("running");
  });

  it("builds the real connector when nobody substituted one", async () => {
    // The default is the connector registry, not a stand-in: a `shell` session
    // that reports `running` here is running the reference connector, and can
    // answer for itself.
    const registry = registryOf({ session_id: "one", connector_type: "shell" });
    const runtimes = new SessionRuntimes(registry);

    await runtimes.startAutoStart();

    expect(observe(registry, "one").lifecycle_state).toBe("running");
    const connector = runtimes.connector("one");
    expect(connector?.isConnected()).toBe(true);
    expect(await connector?.getSnapshot()).toMatchObject({ type: "snapshot" });

    await runtimes.stopAll();
  });
});

describe("bringing sessions back down", () => {
  it("stops what it started, and says when", async () => {
    const registry = registryOf({ session_id: "one" });
    const connector = new FakeConnector();
    let clock = 1_700_000_000;
    const runtimes = new SessionRuntimes(registry, { build: () => connector, now: () => clock });

    await runtimes.start("one");
    clock = 1_700_000_005;
    await runtimes.stopAll();

    expect(connector.stopped).toBe(1);
    // The reference's own shutdown: stopped, with the instant written down.
    expect(observe(registry, "one")).toEqual(golden.stopped);
    expect(registry.status("one")?.stopped_at).toBe(1_700_000_005);
  });

  it("can be started again after it was stopped", async () => {
    const registry = registryOf({ session_id: "one" });
    let built = 0;
    const runtimes = new SessionRuntimes(registry, {
      build: () => {
        built += 1;
        return new FakeConnector();
      },
    });

    await runtimes.start("one");
    await runtimes.stopAll();
    await runtimes.start("one");

    expect(built).toBe(2);
    expect(observe(registry, "one").lifecycle_state).toBe("running");
  });

  it("has nothing to stop when nothing was started", async () => {
    const registry = registryOf({ session_id: "one" });
    const runtimes = new SessionRuntimes(registry);

    await expect(runtimes.stopAll()).resolves.toBeUndefined();

    expect(observe(registry, "one")).toEqual(golden.initial);
  });

  it("has no connector for a session nobody started", () => {
    expect(new SessionRuntimes(registryOf({ session_id: "one" })).connector("one")).toBeUndefined();
  });

  it("reads the wall clock when nobody handed it one", async () => {
    const registry = registryOf({ session_id: "one" });
    const runtimes = new SessionRuntimes(registry, { build: () => new FakeConnector() });
    const before = Date.now() / 1000;

    await runtimes.start("one");
    await runtimes.stopAll();

    const stoppedAt = registry.status("one")?.stopped_at ?? 0;
    // Seconds, as every other instant on this wire is — not milliseconds.
    expect(stoppedAt).toBeGreaterThanOrEqual(before);
    expect(stoppedAt).toBeLessThan(before + 60);
  });
});

/**
 * True only when `A` and `B` are the very same union.
 *
 * A compile-time assertion, which is the only kind available: the type is
 * erased before a test could look at it.
 */
type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;

/**
 * This line does not compile if the server's lifecycle type gains a name the
 * reference does not have — which `paused` was — or loses one it does.
 */
const VOCABULARY_IS_THE_REFERENCES: Same<SessionLifecycle, (typeof SESSION_LIFECYCLES)[number]> = true;

describe("the vocabulary a session's state is named in", () => {
  it("is the reference's four names and no others", () => {
    // `paused` is in neither of the reference's two lifecycle machines: not in
    // `bridge/contracts.py`, which is the one that reaches this wire, and not
    // in the control plane's separate `LifecycleState` either, whose names are
    // waiting|running|stopped|error|deleted.
    expect(golden.lifecycles).toEqual(["stopped", "starting", "running", "error"]);
    expect(golden.lifecycles).not.toContain("paused");
    expect([...SESSION_LIFECYCLES]).toEqual(golden.lifecycles);
    expect(VOCABULARY_IS_THE_REFERENCES).toBe(true);
  });

  it("keeps `error`, which this runtime never assigns, because the reference has it", async () => {
    // `error` lives inside the reference's retry loop and is observable only
    // between attempts. With no retry loop here there is no such moment, so
    // nothing this runtime does can produce it — start, fail, stop and start
    // again, and the name never appears.
    const registry = registryOf({ session_id: "one", connector_type: "no-such-connector" }, { session_id: "two" });
    const runtimes = new SessionRuntimes(registry, {
      build: (_id, _name, type) => {
        if (type === "no-such-connector") {
          throw new Error("unsupported connector_type");
        }
        return new FakeConnector();
      },
    });

    await runtimes.startAutoStart();
    await runtimes.stopAll();
    await runtimes.start("one");

    expect(registry.statuses().map((one) => one.lifecycle_state)).not.toContain("error");
    // Still a name a client of this server has to know how to read.
    expect(SESSION_LIFECYCLES).toContain("error");
  });

  it("names every state a status can actually hold", () => {
    const registry = registryOf({ session_id: "one" });
    const held = SESSION_LIFECYCLES.map((name) => {
      registry.setState("one", { lifecycle_state: name });
      return registry.status("one")?.lifecycle_state;
    });
    expect(held).toEqual(golden.lifecycles);
  });
});
