//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Bringing configured sessions up, and reporting honestly while it happens.
 *
 * Port of the part of `provide.uterm.server.registry.SessionRegistry` that
 * *runs* things — `start_session`, `start_auto_start_sessions`, `shutdown` —
 * together with the lifecycle half of
 * `provide.uterm.server.runtime.HostedSessionRuntime` that those reach.
 *
 * ## What this is, and what it is not
 *
 * The reference's runtime is a worker: it owns a connector *and* attaches to
 * the hub, and it reports `running` and `connected` once both are up. This one
 * does both too — `worker-attach.ts` joins the connector to the hub — so the
 * two fields carry the same two meanings here that they do there.
 *
 * `running` claims the session has been brought up: its connector was built
 * from the definition, `start()` was called on it and returned, and the
 * connector is live and will answer for itself. For the reference `shell`
 * connector — the one the default configuration uses, and the only one this
 * port registers — that is the entire session, because it has no network
 * underneath it.
 *
 * `connected` claims the worker is attached to the hub: the hub holds its
 * socket, will pause it for a lease, and can ask it for a screen. That is what
 * the field means in the reference, and it is a genuinely separate question
 * from the lifecycle one — which is why it is a separate field. Encoding it
 * into `lifecycle_state` would collapse two questions into one and lose both
 * answers; online and offline are not lifecycle states.
 *
 * The one thing neither claims is that a *browser* can reach the session. This
 * server binds no WebSocket, so nothing renders it live. That shows up as an
 * empty presence set, not as a lifecycle or a connection.
 *
 * ## Where a failed start comes to rest, and why it is not `error`
 *
 * A start that fails is recorded as `stopped`, with the message in
 * `last_error` and the instant in `stopped_at`. That is the reference's own
 * resting place, recorded in `sessionruntime_golden.json`.
 *
 * `error` looks like the obvious name and is the wrong one. In
 * `HostedSessionRuntime._run` (runtime.py, ~425-482) `_state = "error"` is
 * assigned *inside* the retry loop, so it is only ever observable between
 * attempts: a failure classified permanent breaks out of the loop and the line
 * after it assigns `stopped` and `_stopped_at`, while a transient one sleeps a
 * backoff and comes round to `starting` again. Nothing ever rests at `error`.
 *
 * So the field that separates "stopped because nobody asked it to run" from
 * "stopped because it tried and failed" is `last_error`, not the state — which
 * is exactly why both are written here. Reporting `error` instead would have
 * meant clients switching on `lifecycle_state` see a value the reference never
 * leaves them looking at.
 *
 * `error` stays in the vocabulary because it is in the reference's, and a
 * client must be able to name what it may be sent. This runtime has no retry
 * loop, so it never assigns it.
 */

import { buildConnector, type SessionConnector } from "../connectors/index.ts";
import type { InputMode } from "../hub/index.ts";
import type { SessionHub } from "./session-hub.ts";
import type { SessionRegistry } from "./session-registry.ts";
import { type AttachedWorker, attachConnector } from "./worker-attach.ts";

/** How a connector is built for one session. The connector registry's shape. */
export type ConnectorBuilder = (
  sessionId: string,
  displayName: string,
  connectorType: string,
  config: Record<string, unknown>,
) => SessionConnector;

/** What a runtime is wired with. The real thing unless a test says otherwise. */
export interface SessionRuntimeOptions {
  /** How a connector is built. The connector registry by default. */
  build?: ConnectorBuilder | undefined;
  /** The clock, in seconds — the unit every instant on this wire is in. */
  now?: (() => number) | undefined;
}

/** What went wrong, as `last_error` carries it. */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * The sessions this server has actually brought up.
 *
 * Separate from {@link SessionRegistry}, which holds what a session *is* and
 * what state it is in: this holds the live connectors and is the only thing
 * that moves a session between states. The registry's `setState` is the seam
 * between them, so the read surface never has to know a runtime exists.
 */
export class SessionRuntimes {
  readonly #registry: SessionRegistry;
  readonly #hub: SessionHub;
  readonly #build: ConnectorBuilder;
  readonly #now: () => number;
  /** Only sessions that are up: one that failed to start is not in here. */
  readonly #connectors = new Map<string, SessionConnector>();
  /** The hub attachment for each running session, for taking it back off. */
  readonly #attached = new Map<string, AttachedWorker>();

  constructor(registry: SessionRegistry, hub: SessionHub, options: SessionRuntimeOptions = {}) {
    this.#registry = registry;
    this.#hub = hub;
    this.#build = options.build ?? buildConnector;
    this.#now = options.now ?? (() => Date.now() / 1000);
  }

  /** The live connector for a session, or nothing when it is not up. */
  connector(sessionId: string): SessionConnector | undefined {
    return this.#connectors.get(sessionId);
  }

  /**
   * Tell a running connector its input mode changed.
   *
   * A session that is not up is not an error: the definition and the hub have
   * already recorded the new mode, and the connector will be built in it when
   * the session next starts. The reference reaches its connector through the
   * runtime for the same reason — the mode is a property of the session, and
   * the connector is only one of the places holding a copy.
   */
  async setMode(sessionId: string, mode: InputMode): Promise<void> {
    await this.#connectors.get(sessionId)?.setMode(mode);
  }

  /**
   * Bring one session up.
   *
   * Never throws: a session that cannot start says so in its own state, which
   * is the only way a caller starting several of them can carry on past one.
   */
  async start(sessionId: string): Promise<void> {
    const definition = this.#registry.definition(sessionId);
    // A start for a session that was deleted under the caller is a race, not a
    // fault — the same tolerance `setState` has for the same reason.
    if (definition === undefined) {
      return;
    }
    // The reference returns early on a task that is still running: a second
    // start must not rebuild the connector or rewind the state.
    if (this.#connectors.has(sessionId)) {
      return;
    }

    // `starting`, and last time's outcome cleared with it — what failed before
    // is not what is happening now. The reference's `start()` sets all three.
    this.#registry.setState(sessionId, {
      lifecycle_state: "starting",
      stopped_at: null,
      last_error: null,
    });

    try {
      const connector = this.#build(
        sessionId,
        definition.display_name,
        definition.connector_type,
        // Copied: a connector that normalised its settings in place would
        // otherwise rewrite the definition every request is answered from.
        { ...definition.connector_config },
      );
      await connector.start();
      // Recorded only once it is up, so nothing is ever left holding a
      // connector that threw on the way.
      this.#connectors.set(sessionId, connector);
      // Attached to the hub as a worker, which is what makes the session
      // leasable: the hub arbitrates over workers, and one that had merely
      // been started would be refused every acquire with `no_worker`.
      this.#attached.set(
        sessionId,
        await attachConnector(this.#hub, sessionId, connector, definition.input_mode, { now: this.#now }),
      );
      this.#registry.setState(sessionId, { lifecycle_state: "running", connected: true });
    } catch (error) {
      // `stopped`, with the reason and the instant — where the reference's run
      // loop comes to rest when it gives up. See the note on `error` above.
      this.#registry.setState(sessionId, {
        lifecycle_state: "stopped",
        last_error: errorText(error),
        stopped_at: this.#now(),
      });
    }
  }

  /**
   * Bring up every session the configuration flagged `auto_start`.
   *
   * `registry.start_auto_start_sessions()`, which the reference runs from its
   * application lifespan. A port that stored the flag and never acted on it
   * would report "not running" to every client for a session the operator
   * asked to have running — while echoing `auto_start: true` back at them in
   * the same object.
   *
   * In configuration order, and one that will not come up does not cost the
   * rest their boot.
   */
  async startAutoStart(): Promise<void> {
    for (const definition of this.#registry.definitions()) {
      if (definition.auto_start) {
        await this.start(definition.session_id);
      }
    }
  }

  /**
   * Bring every session back down. The reference's `registry.shutdown()`.
   *
   * Something that starts connectors has to be able to release them, or a
   * server that stopped listening would still be holding whatever they hold.
   */
  async stopAll(): Promise<void> {
    for (const [sessionId, connector] of this.#connectors) {
      // Detached before it is stopped, so nothing can take a lease on a
      // worker whose connector is on its way down.
      await this.#attached.get(sessionId)?.detach();
      await connector.stop();
      this.#registry.setState(sessionId, { lifecycle_state: "stopped", connected: false, stopped_at: this.#now() });
    }
    this.#attached.clear();
    this.#connectors.clear();
  }
}
