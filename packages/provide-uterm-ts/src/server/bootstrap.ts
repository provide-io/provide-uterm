//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A configuration document in, a running application out.
 *
 * Port of the wiring `provide.uterm.server.app.create_server_app` does before
 * anything is served: validate the auth mode, stand the stub identity provider
 * up when the mode asks for one, fill the session definitions out of the
 * configuration, and hand the result to the router.
 *
 * The `dev_token` guard is carried over exactly. That mode is only safe
 * because the address it binds is one nothing else can reach, and the
 * reference refuses to start rather than bind it anywhere routable — a
 * refusal, not a warning, because a warning in a startup log is a thing
 * nobody reads until afterwards.
 */

import { type AuthSettings, type DevIdpAuthConfig, setupDevIdp } from "../serverauth/index.ts";
import { deepMerge, normalizeDocument, SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import { createServerApp, type ServerApp } from "./app.ts";
import { SessionRegistry } from "./session-registry.ts";
import { sessionDefinitionFrom } from "./session-status.ts";

/**
 * The version this server reports.
 *
 * Held against `package.json` by a test rather than read from it, so nothing
 * in the request path has to reach a filesystem a Worker does not have.
 */
export const SERVER_VERSION = "0.4.0";

/** A configuration that will not start a server. */
export class ServerBootstrapError extends Error {}

/**
 * The addresses `dev_token` mode is permitted on.
 *
 * Exported so a caller can say which they are, and so the refusal below can be
 * held to the same list it names in its message.
 */
export const SERVER_BOOTSTRAP_HOST: readonly string[] = ["127.0.0.1", "localhost", "::1"];

const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(SERVER_BOOTSTRAP_HOST);

/** How to bootstrap. */
export interface BootstrapOptions {
  /** A configuration document, merged over the defaults. */
  document?: Readonly<Record<string, unknown>> | undefined;
  /** Overrides the document's `auth.mode`, as a command line would. */
  authMode?: string | undefined;
  /** The clock, in seconds. The runtime's own unless a test says otherwise. */
  now?: (() => number) | undefined;
  /** Where the stub IdP's secrets come from. The runtime's own by default. */
  randomToken?: ((bytes: number) => string) | undefined;
}

/** Everything a caller needs once the server is assembled. */
export interface BootstrappedServer {
  app: ServerApp;
  registry: SessionRegistry;
  /** The `auth` section after the stub IdP rewrote it, if it did. */
  auth: DevIdpAuthConfig & AuthSettings;
  /**
   * The token the stub IdP minted, or the empty string in any other mode.
   *
   * A conformance driver announces it; a person reads it out of the file the
   * reference writes. Either way it is a real credential and goes through the
   * real validator.
   */
  token: string;
  /** The merged configuration, for whatever else needs to read it. */
  config: Readonly<Record<string, unknown>>;
}

/**
 * One section of a merged document.
 *
 * Always there, and read without a fallback: the defaults define every
 * section and every field in it, so a `?? {}` here would be a branch nothing
 * can take and a place for a genuinely missing section to hide. A document
 * whose section is not a table is `serverconfig`'s to refuse, and it does,
 * before this is reached.
 */
function section(document: Readonly<Record<string, unknown>>, name: string): Record<string, unknown> {
  return document[name] as Record<string, unknown>;
}

/**
 * Assemble a server.
 *
 * @throws {ServerBootstrapError} When the auth mode is one that was removed,
 *   or when `dev_token` was asked for on an address that is not loopback.
 */
export function bootstrapServer(options: BootstrapOptions = {}): BootstrappedServer {
  const config = deepMerge(SERVER_CONFIG_DEFAULTS, normalizeDocument(options.document ?? {}));
  // Copied before anything writes to it. A document that overrode nothing in
  // `[auth]` leaves the *defaults* object itself in the merge, and the
  // defaults are frozen precisely so one server's stub IdP cannot become the
  // next one's configuration.
  const auth = { ...section(config, "auth") } as unknown as DevIdpAuthConfig & AuthSettings;
  config.auth = auth;
  if (options.authMode !== undefined) {
    auth.mode = options.authMode;
  }

  const mode = String(auth.mode).trim().toLowerCase();
  let token = "";
  if (mode === "dev_token") {
    const host = String(section(config, "server").host).trim().toLowerCase();
    if (!LOOPBACK_HOSTS.has(host)) {
      throw new ServerBootstrapError(
        "auth.mode='dev_token' is only permitted when server.host is a loopback address " +
          `(127.0.0.1, localhost, or ::1). Got: ${host}`,
      );
    }
    // Rewrites `auth` in place, leaving the mode as `jwt`: from here on there
    // is one authentication path in the process, and it is the production one.
    token = setupDevIdp(auth, { now: options.now, randomToken: options.randomToken }).token;
  } else if (mode === "dev" || mode === "none") {
    throw new ServerBootstrapError(
      "AUTH_MODE=dev and 'none' have been removed for security reasons. " +
        "Use 'dev_token' for local development or 'jwt' for production.",
    );
  }

  const createdAt = new Date(Math.trunc((options.now ?? (() => Date.now() / 1000))() * 1000)).toISOString();
  const entries = config.sessions as Readonly<Record<string, unknown>>[];
  const registry = new SessionRegistry(
    entries.map((entry) => sessionDefinitionFrom(entry, createdAt)),
    Boolean(section(config, "recording").enabled_by_default),
  );

  const app = createServerApp({
    registry,
    auth,
    version: SERVER_VERSION,
    controlPlaneBackend: String(section(config, "control_plane").backend),
    startupTime: (options.now ?? (() => Date.now() / 1000))(),
    now: options.now,
  });
  return { app, registry, auth, token, config };
}
