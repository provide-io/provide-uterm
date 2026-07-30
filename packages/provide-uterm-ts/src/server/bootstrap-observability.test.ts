//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a server assembled by {@link bootstrapServer} observes about itself.
 *
 * Every one of these drives the *hosted* factory and provokes the counted
 * event over HTTP, because that is the only shape of test that can fail when
 * the wiring is missing. A counter is emitted by code that reads correctly at
 * every call site — `hub.store.metric("...")` — and is discarded in silence
 * when the store was built without a sink, so a test that asserts the sink is
 * *present* passes on a hub nobody handed one to as long as somebody hands one
 * to the double. This is the defect that shipped in the C# port, where the
 * hosted factory never assigned `TermHubConfig.OnMetric`.
 *
 * The rate limiter is driven rather than waited out: the configuration is
 * lowered to one acquire per second, so the second acquire in the same instant
 * is over budget on any machine, however loaded. Nothing here sleeps.
 */

import { describe, expect, it } from "vitest";
import type { Logger } from "../telemetry/index.ts";
import { bootstrapServer } from "./bootstrap.ts";

/** The base a `Request` is built against. Never reaches the wire. */
const BASE = "http://127.0.0.1:0";

/** One address, so every request in a test charges the same bucket. */
const CALLER = "10.0.0.1";

/** A POST to the acquire route, as the credential the stub IdP minted. */
function acquire(token: string): Request {
  return new Request(`${BASE}/worker/provide-shell/hijack/acquire`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: "{}",
  });
}

/** A hosted server whose acquire budget is one per second. */
function hosted(overrides: Record<string, unknown> = {}) {
  return bootstrapServer({
    authMode: "dev_token",
    now: () => 1_700_000_000,
    document: { rest_acquire_rate_limit_per_sec: 1 },
    ...overrides,
  });
}

/** A worker socket that accepts everything and remembers nothing. */
const silentWorker = { sendText: async () => {} };

/** A logger that keeps what it was told, and the records it kept. */
function recording(): { logger: Logger; records: { fields: Record<string, unknown>; msg?: string }[] } {
  const records: { fields: Record<string, unknown>; msg?: string }[] = [];
  const logger: Logger = {
    trace: () => {},
    debug: () => {},
    info: () => {},
    warn: (fields, msg) => {
      records.push(msg === undefined ? { fields } : { fields, msg });
    },
    error: () => {},
    child: () => logger,
  };
  return { logger, records };
}

describe("the counters a hosted server keeps", () => {
  it("counts an acquire it refused for rate", async () => {
    const { app, metrics, token } = hosted();

    // The first spends the whole budget; the next two are over it. All three go
    // through the application, so the counter has to survive the trip from the
    // route that emits it to whatever the factory pointed the store at. Two
    // refusals rather than one because a counter that is *set* rather than
    // accumulated would answer 1 to either, and 1 is the answer a first
    // increment gives however the second is handled.
    expect((await app.handle(acquire(token), CALLER)).status).toBe(409);
    expect((await app.handle(acquire(token), CALLER)).status).toBe(429);
    expect((await app.handle(acquire(token), CALLER)).status).toBe(429);

    expect(metrics.get("rest_acquire_rate_limited_total")).toBe(2);
  });

  it("counts an acquire it granted", async () => {
    const { app, hub, metrics, token } = hosted();
    hub.registerWorker("provide-shell", silentWorker, "hijack");

    expect((await app.handle(acquire(token), CALLER)).status).toBe(200);

    expect(metrics.get("hijack_acquires_total")).toBe(1);
  });

  it("counts a lease it expired, which the lease manager reports through the hub", async () => {
    const { app, hub, metrics, token } = hosted();
    hub.registerWorker("provide-shell", silentWorker, "hijack");
    expect((await app.handle(acquire(token), CALLER)).status).toBe(200);

    // The lease manager's counters travel a longer route than the routes' own:
    // out through the callback this hub handed it, back into the store, and only
    // then to the sink. Expiry is reached by moving the deadline rather than by
    // driving HTTP because the shortest lease the hub grants is a whole second
    // and `bootstrapServer` builds its own monotonic clock — a test that waited
    // one out would be a test that spends a second to observe an increment.
    const held = hub.registry.get("provide-shell")?.hijackSession as { leaseExpiresAt: number };
    held.leaseExpiresAt = 0;
    expect(await hub.lease.cleanupExpired("provide-shell")).toBe(true);

    expect(metrics.get("hijack_lease_expiries_total")).toBe(1);
  });
});

describe("the hooks a hosted server exposes", () => {
  it("tells a configured subscriber that a worker became hijacked", async () => {
    const seen: [string, boolean, string | undefined][] = [];
    const { app, hub, token } = hosted({
      onHijackChanged: (workerId: string, enabled: boolean, owner?: string) => {
        seen.push([workerId, enabled, owner]);
      },
    });
    hub.registerWorker("provide-shell", silentWorker, "hijack");

    expect((await app.handle(acquire(token), CALLER)).status).toBe(200);

    expect(seen).toEqual([["provide-shell", true, "operator"]]);
  });

  it("logs through the configured logger when a subscriber rejects", async () => {
    const { logger, records } = recording();
    const { app, hub, token } = hosted({
      logger,
      onHijackChanged: async () => {
        throw new Error("subscriber exploded");
      },
    });
    hub.registerWorker("provide-shell", silentWorker, "hijack");

    expect((await app.handle(acquire(token), CALLER)).status).toBe(200);
    // The rejection is caught in a `catch` handler, so it is one microtask
    // behind the response.
    await Promise.resolve();

    expect(records.map((record) => record.msg)).toEqual(["on_hijack_changed callback raised"]);
  });
});
