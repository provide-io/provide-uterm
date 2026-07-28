//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Connector self-registration.
 *
 * Port of the Python module `provide.uterm.server.connectors.registry`.
 *
 * An unknown type is a refusal rather than a default: a session created with
 * a typo must not silently land on some other transport.
 */

import type { SessionConnector } from "./base.ts";
import { ShellSessionConnector } from "./shell.ts";

/** Builds a connector for one session. */
export type SessionConnectorFactory = (
  sessionId: string,
  displayName: string,
  config: Record<string, unknown>,
) => SessionConnector;

/**
 * The types the reference ships with.
 *
 * The network connectors are named here so the set matches the reference even
 * though only the reference connector is built in this port so far.
 */
export const BUILTIN_CONNECTOR_TYPES: readonly string[] = ["shell", "ssh", "telnet", "websocket"];

const registry = new Map<string, SessionConnectorFactory>([
  ["shell", (sessionId, displayName, config) => new ShellSessionConnector(sessionId, displayName, config)],
]);

/**
 * Register a factory under a type name.
 *
 * A later registration replaces an earlier one: that is how a deployment
 * substitutes its own transport for a built-in.
 */
export function registerConnector(name: string, factory: SessionConnectorFactory): void {
  registry.set(name, factory);
}

/**
 * Build a connector by type name.
 *
 * @throws {Error} On a type nothing has registered.
 */
export function buildConnector(
  sessionId: string,
  displayName: string,
  connectorType: string,
  config: Record<string, unknown>,
): SessionConnector {
  const factory = registry.get(connectorType);
  if (factory === undefined) {
    throw new Error(`unsupported connector_type: '${connectorType}'`);
  }
  return factory(sessionId, displayName, config);
}

/** The type names currently registered. */
export function registeredTypes(): ReadonlySet<string> {
  return new Set(registry.keys());
}
