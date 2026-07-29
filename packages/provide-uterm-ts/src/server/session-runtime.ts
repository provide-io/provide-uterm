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
 * The reference's runtime is a worker: it owns a connector *and* dials the hub
 * over a WebSocket, and it reports `running` once both are up. This one owns
 * the connector and stops there. That is the whole of the difference, and it
 * is deliberate — but it means `running` here has to be read for exactly what
 * it claims.
 *
 * It claims the session has been brought up: its connector was built from the
 * definition, `start()` was called on it and returned, and the connector is
 * live and will answer for itself. For the reference `shell` connector — the
 * one the default configuration uses, and the only one this port registers —
 * that is the entire session, because it has no network underneath it. So a
 * `running` reported here is a session that can be snapshotted, typed at and
 * asked for its analysis. Nothing is being claimed that is not true.
 *
 * It does *not* claim a client can reach it. This server binds the read half
 * of the session API and no terminal transport at all, so nothing has attached
 * and `connected` stays false — which is what that separate field is for.
 * Encoding "a client is attached" into `lifecycle_state` would collapse two
 * questions into one field and lose both answers; online and offline are not
 * lifecycle states.
 *
 * ## The one deliberate difference in a state name
 *
 * A start that fails is recorded as `error`, with the message. The reference
 * reaches `stopped` in the same situation — recorded in
 * `sessionruntime_golden.json` and asserted in the tests — but only because it
 * falls out of a *retry loop* on a permanent failure, and `error` is what it
 * reports for the whole time it is still retrying. This runtime does not
 * retry, so there is no "will try again" for `error` to mean and no
 * give-up-and-stop for `stopped` to mean either. `error` is the name in the
 * reference's own vocabulary for a connector that failed, and it is what the
 * C# port reports, so it is what this reports.
 */

import { buildConnector, type SessionConnector } from "../connectors/index.ts";
import type { SessionRegistry } from "./session-registry.ts";

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
  readonly #build: ConnectorBuilder;
  readonly #now: () => number;
  /** Only sessions that are up: one that failed to start is not in here. */
  readonly #connectors = new Map<string, SessionConnector>();

  constructor(registry: SessionRegistry, options: SessionRuntimeOptions = {}) {
    this.#registry = registry;
    this.#build = options.build ?? buildConnector;
    this.#now = options.now ?? (() => Date.now() / 1000);
  }

  /** The live connector for a session, or nothing when it is not up. */
  connector(sessionId: string): SessionConnector | undefined {
    return this.#connectors.get(sessionId);
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
      this.#registry.setState(sessionId, { lifecycle_state: "running" });
    } catch (error) {
      this.#registry.setState(sessionId, { lifecycle_state: "error", last_error: errorText(error) });
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
      await connector.stop();
      this.#registry.setState(sessionId, { lifecycle_state: "stopped", stopped_at: this.#now() });
    }
    this.#connectors.clear();
  }
}
