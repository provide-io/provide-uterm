//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a server says about itself when nobody has authenticated.
 *
 * Port of `provide.uterm.server.routes.health`.
 *
 * These endpoints carry no authentication, deliberately: a load balancer has
 * no token, and a server that refused an anonymous probe is a server nothing
 * can route to. The corollary is the one thing they must never do — a probe
 * carrying a token that does not verify must still get a 200, or every
 * deployment would fall out of its load balancer during a key rotation.
 *
 * Liveness and readiness are separate answers to separate questions.
 * `/healthz` says the process is up. `/readyz` and the `ready` field say
 * startup finished: until it has, a 503 keeps traffic away from a pod that
 * would otherwise answer requests with half a server.
 */

import { pyRoundTo } from "../pycompat/index.ts";

/** What the health endpoints read off the running server. */
export interface HealthState {
  /** Whether the session registry has been attached at all. */
  registryAttached: boolean;
  /** Whether startup finished: migrations run, background tasks started. */
  ready: boolean;
  /** When the process started, in seconds, or zero when it has not been recorded. */
  startupTime: number;
  /** The package version this server reports. */
  version: string;
  /** How many sessions the registry holds. */
  activeSessions: number;
  /** Which store is behind the control plane. */
  controlPlaneBackend: string;
  /** The current time in seconds. */
  now: number;
}

/** A status code and the body that goes with it. */
export interface HealthAnswer {
  status: number;
  body: Record<string, unknown>;
}

/** The rich health report, and the two ways it is not ready to give one. */
export function healthReport(state: HealthState): HealthAnswer {
  if (!state.registryAttached) {
    return { status: 503, body: { status: "unavailable", ok: false, ready: false, service: "uterm-server" } };
  }
  // A registry attached is not a server that finished starting. A pod that
  // answered 200 here between the two would take traffic it cannot serve.
  if (!state.ready) {
    return { status: 503, body: { status: "starting", ok: false, ready: false, service: "uterm-server" } };
  }
  return {
    status: 200,
    body: {
      status: "ok",
      ok: true,
      ready: true,
      service: "uterm-server",
      version: state.version,
      uptime_s: uptimeSeconds(state),
      active_sessions: state.activeSessions,
      control_plane_backend: state.controlPlaneBackend,
    },
  };
}

/**
 * How long the server has been up, to two decimal places.
 *
 * Rounded the way the reference rounds — CPython's `round`, which breaks a
 * tie to even rather than away from zero. A dashboard would not notice; a
 * matrix comparing two servers field-for-field would.
 */
export function uptimeSeconds(state: HealthState): number {
  return state.startupTime > 0 ? pyRoundTo(state.now - state.startupTime, 2) : 0.0;
}

/** The minimal liveness probe: no state, no dependencies, always 200. */
export function livenessReport(): HealthAnswer {
  return { status: 200, body: { status: "ok" } };
}

/** The readiness probe, which is a 503 until startup finished. */
export function readinessReport(ready: boolean): HealthAnswer {
  return ready ? { status: 200, body: { status: "ready" } } : { status: 503, body: { status: "not_ready" } };
}
