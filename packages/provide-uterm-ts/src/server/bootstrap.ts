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

import { effectiveAllowLoopbackDestinations, type WebhookEgressConfig } from "../egress/index.ts";
import {
  applyCfAccessTeamDomain,
  type AuthSettings,
  type DevIdpAuthConfig,
  setupDevIdp,
} from "../serverauth/index.ts";
import { deepMerge, normalizeDocument, SERVER_CONFIG_DEFAULTS } from "../serverconfig/index.ts";
import type { Logger } from "../telemetry/index.ts";
import { createServerApp, type ServerApp } from "./app.ts";
import { SessionHub } from "./session-hub.ts";
import { SessionRegistry } from "./session-registry.ts";
import { SessionRuntimes } from "./session-runtime.ts";
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
  /**
   * Where the hub reports a callback that failed. The runtime's by default.
   *
   * Reachable so an embedder can route this server's output into their own
   * logging rather than have it written to stderr from under them.
   */
  logger?: Logger | undefined;
  /**
   * Notified when a worker's hijack state changes.
   *
   * Nothing shipped subscribes, and the reference's hosted factory leaves the
   * equivalent hook unassigned for the same reason — the transitions are
   * already carried by the events log and the hijack-state broadcast. It is
   * accepted here so the hook is *reachable* from the hosted path instead of
   * being a parameter only a hand-built hub could ever pass.
   */
  onHijackChanged?: ((workerId: string, enabled: boolean, owner?: string) => void | Promise<void>) | undefined;
}

/** Everything a caller needs once the server is assembled. */
export interface BootstrappedServer {
  app: ServerApp;
  registry: SessionRegistry;
  /**
   * The hub this server's leases are arbitrated through.
   *
   * Exposed because a caller that wants to inspect who holds what has nowhere
   * else to ask, and because the runtimes and the application share exactly
   * this one — a second hub would arbitrate separately over the same terminal.
   */
  hub: SessionHub;
  /**
   * The sessions this server can bring up, and the thing that brings them.
   *
   * Assembled but not started: the reference starts its own from the
   * application lifespan, once the process is committed to serving. Whoever
   * binds the socket calls {@link SessionRuntimes.startAutoStart} — see
   * `conformance/serve.ts` — and {@link SessionRuntimes.stopAll} on the way
   * out. Bootstrapping stays synchronous, and a caller that only wants to
   * *inspect* a configuration does not start connectors by doing so.
   */
  runtimes: SessionRuntimes;
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
  /**
   * Every counter this server has emitted, and what it stands at.
   *
   * The port of the reference factory's own `metrics` dict: it keeps the
   * counters beside the application it assembled and hands the hub a closure
   * that increments them. Live rather than a snapshot — a caller holding this
   * sees each increment as it happens, which is what a `/api/metrics` handler
   * would need and what a test needs to assert a counter was observed at all.
   *
   * Counters appear when they are first emitted, where the reference pre-seeds
   * every known name at zero. The reference has to: it serves `/api/metrics`
   * and `/api/metrics/prometheus`, and an absent name there would read as a
   * counter that does not exist rather than one that has not fired. This port
   * serves neither route yet, so nothing is owed the zeros, and a map holding
   * only what actually happened cannot claim a counter this port never emits.
   */
  metrics: ReadonlyMap<string, number>;
  /**
   * Whether this server may deliver a webhook to a loopback destination.
   *
   * `webhooks.allow_loopback_destinations`, *or* a loopback bind — §3 of
   * `conformance/EGRESS_GUARD.md`. Computed here, once, because this is where
   * the server is built from configuration: a permission recomputed at each
   * call site is a permission that ends up answered two different ways, and
   * the bind term in particular is easy to forget in one of them.
   *
   * Exposed rather than held privately because the thing that consumes it —
   * webhook registration and delivery — is not in this port yet. Whoever adds
   * it takes this value; it must not derive its own.
   */
  allowLoopbackDestinations: boolean;
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
  // Runs unconditionally, before the mode branch below — matching Go, C#
  // and Python, which all apply the Cloudflare Access team-domain fill as
  // part of AuthConfig validation regardless of auth.mode.
  applyCfAccessTeamDomain(auth as unknown as Record<string, unknown>);
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

  // The hub is built before the application and the runtimes because both hold
  // it: the lease routes arbitrate through it, and a session that starts
  // attaches to it as a worker. One hub per server — two would arbitrate
  // separately over the same terminal.
  // The REST ceilings are read straight off the merged document: every field
  // has a default, so a document that never mentions them still supplies the
  // reference's own numbers, and one that does supplies numbers the schema has
  // already refused if they were unusable.
  // The counters are owned here, by the thing that assembled the server, and
  // the hub is handed a closure over them — the same arrangement as the
  // reference factory's `metrics` dict and `_inc_metric`. Handing the hub a
  // sink is not optional decoration: the store discards every counter when it
  // has none, so a hosted server that left this out would emit its whole
  // metrics surface into nothing while every call site still read correctly.
  const metrics = new Map<string, number>();
  const hub = new SessionHub({
    wallNow: options.now,
    restAcquireRate: config.rest_acquire_rate_limit_per_sec as number,
    restSendRate: config.rest_send_rate_limit_per_sec as number,
    logger: options.logger,
    onMetric: (name, value) => {
      metrics.set(name, (metrics.get(name) ?? 0) + value);
    },
    onHijackChanged: options.onHijackChanged,
  });
  const runtimes = new SessionRuntimes(registry, hub, { now: options.now });
  const app = createServerApp({
    registry,
    auth,
    hub,
    connectors: runtimes,
    version: SERVER_VERSION,
    controlPlaneBackend: String(section(config, "control_plane").backend),
    startupTime: (options.now ?? (() => Date.now() / 1000))(),
    now: options.now,
  });
  // Read off the merged document, so a deployment that never mentioned
  // `[webhooks]` still gets the reference's own default and the bind term.
  const allowLoopbackDestinations = effectiveAllowLoopbackDestinations(config as unknown as WebhookEgressConfig);
  return { app, registry, hub, runtimes, auth, token, config, metrics, allowLoopbackDestinations };
}
