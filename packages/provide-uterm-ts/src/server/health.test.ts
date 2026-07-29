//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The health, liveness and readiness answers.
 *
 * The bodies a running server gives are held to `serverhttp_golden` by
 * `app.test.ts`. What is here is the two states the corpus cannot reach —
 * a server whose registry never attached, and one whose startup has not
 * finished — and the rounding, which is CPython's rather than this runtime's.
 */

import { describe, expect, it } from "vitest";
import { pyRoundTo } from "../pycompat/index.ts";
import { type HealthState, healthReport, livenessReport, readinessReport, uptimeSeconds } from "./health.ts";

/** A server that is up and finished starting. */
function state(overrides: Partial<HealthState> = {}): HealthState {
  return {
    registryAttached: true,
    ready: true,
    startupTime: 1000,
    version: "9.9.9",
    activeSessions: 2,
    controlPlaneBackend: "memory",
    now: 1002.5,
    ...overrides,
  };
}

describe("the rich health report", () => {
  it("says unavailable when no registry was ever attached", () => {
    // Before anything else: a process with no registry cannot answer for a
    // session, and reporting `ok` would put it into a load balancer's pool.
    expect(healthReport(state({ registryAttached: false }))).toEqual({
      status: 503,
      body: { status: "unavailable", ok: false, ready: false, service: "uterm-server" },
    });
  });

  it("says starting when the registry is there and startup is not finished", () => {
    // Two different 503s, because they are two different things to debug.
    expect(healthReport(state({ ready: false }))).toEqual({
      status: 503,
      body: { status: "starting", ok: false, ready: false, service: "uterm-server" },
    });
  });

  it("reports booleans, not the number one and not the string true", () => {
    const body = healthReport(state()).body;
    expect(body.ok).toBe(true);
    expect(body.ready).toBe(true);
    expect(typeof body.ok).toBe("boolean");
  });

  it("names the service, so a probe can tell it from whatever else is on the port", () => {
    expect(healthReport(state()).body.service).toBe("uterm-server");
  });

  it("reports the store behind the control plane", () => {
    expect(healthReport(state({ controlPlaneBackend: "sqlite" })).body.control_plane_backend).toBe("sqlite");
  });
});

describe("the uptime", () => {
  it("is zero when nothing recorded a start", () => {
    // Not a negative number counted from the epoch, which is what subtracting
    // an unset start would give.
    expect(uptimeSeconds(state({ startupTime: 0 }))).toBe(0);
  });

  it("is the elapsed seconds to two places", () => {
    expect(uptimeSeconds(state({ startupTime: 1000, now: 1002.5 }))).toBe(2.5);
  });

  it("rounds the way the reference rounds, which is to even", () => {
    // CPython's `round` breaks a tie to even rather than away from zero. A
    // dashboard would not notice; a matrix comparing two servers would.
    // 0.125 is exactly representable, so the tie is real: CPython answers
    // 0.12 and this runtime's own `toFixed` answers 0.13.
    const value = uptimeSeconds(state({ startupTime: 1, now: 1.125 }));
    expect(value).toBe(pyRoundTo(0.125, 2));
    expect(value).toBe(0.12);
    expect(value).not.toBe(Number((0.125).toFixed(2)));
  });
});

describe("the probes", () => {
  it("says the process is alive with no state at all", () => {
    expect(livenessReport()).toEqual({ status: 200, body: { status: "ok" } });
  });

  it("says ready once startup finished, and 503 until then", () => {
    expect(readinessReport(true)).toEqual({ status: 200, body: { status: "ready" } });
    expect(readinessReport(false)).toEqual({ status: 503, body: { status: "not_ready" } });
  });
});
