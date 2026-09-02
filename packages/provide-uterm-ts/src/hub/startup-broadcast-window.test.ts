//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Frames broadcast while a browser is still starting up must not be lost.
 *
 * A browser registers with deferBroadcast so its hello, hijack_state and
 * presence_sync arrive before anything else; until it is activated it is not in
 * the broadcast set at all, and what was broadcast meanwhile used to be dropped
 * outright — this port had the pending set but no buffer behind it.
 *
 * Port of test_startup_broadcast_window.py.
 */

import { describe, expect, it } from "vitest";
import {
  type ConnectionHubCallbacks,
  ConnectionManager,
  MessageRouter,
  type RouterHubCallbacks,
  STARTUP_BUFFER_MAX_FRAMES,
  WorkerRegistry,
  type WorkerTermState,
} from "./index.ts";
import type { Connection } from "./models.ts";

const WORKER = "w1";

function httpReq(id: string, url: string): Record<string, unknown> {
  return { type: "http_req", id, method: "GET", url, _channel: "http" };
}

/** A browser socket that records what it was sent, or refuses to take it. */
class FakeBrowser {
  readonly sent: string[] = [];
  #throws = false;

  failWrites(): void {
    this.#throws = true;
  }

  async sendText(payload: string): Promise<void> {
    if (this.#throws) {
      throw new Error("socket closed");
    }
    this.sent.push(payload);
  }
}

/** One fake standing in for both hub surfaces, so they share the same state. */
class FakeHub implements RouterHubCallbacks, ConnectionHubCallbacks {
  readonly registry = new WorkerRegistry<WorkerTermState>();
  readonly startupPendingBrowsers = new Set<Connection>();
  readonly startupPendingFrames = new Map<Connection, Record<string, unknown>[]>();
  maxWorkers = 100;
  maxConnectionsPerPrincipal = 100;

  isHijacked(): boolean {
    return false;
  }

  isDashboardHijackActive(): boolean {
    return false;
  }

  hasValidRestLease(): boolean {
    return false;
  }

  async removeDeadBrowsers(): Promise<boolean> {
    return false;
  }
}

/** A hub with one worker and one browser held mid-handshake. */
async function pending() {
  const hub = new FakeHub();
  const router = new MessageRouter({ hub, sendTimeoutS: 0.05 });
  const connections = new ConnectionManager({ hub, now: () => 0 });
  const browser = new FakeBrowser();
  await connections.registerBrowser(WORKER, browser, "viewer", { deferBroadcast: true });
  return { hub, router, connections, browser };
}

/** Which of `needles` the browser was actually sent, in order. */
function seen(browser: FakeBrowser, ...needles: string[]): string[] {
  return browser.sent.flatMap((payload) => needles.filter((needle) => payload.includes(needle)));
}

describe("the broadcast window a connecting browser cannot see", () => {
  it("delivers an inspect frame sent during startup on activation", async () => {
    const { router, connections, browser } = await pending();

    await router.broadcast(WORKER, httpReq("r1", "/api/users"));
    expect(browser.sent).toEqual([]);

    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(seen(browser, "/api/users")).toEqual(["/api/users"]);
  });

  it("keeps buffered inspect frames in arrival order", async () => {
    const { router, connections, browser } = await pending();

    for (const index of [0, 1, 2]) {
      await router.broadcast(WORKER, httpReq(`r${index}`, `/api/${index}`));
    }
    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(seen(browser, "/api/0", "/api/1", "/api/2")).toEqual(["/api/0", "/api/1", "/api/2"]);
  });

  it("does not replay terminal output from the window", async () => {
    // The hello's initial_snapshot already covers it; replaying prints twice.
    const { router, connections, browser } = await pending();

    await router.broadcast(WORKER, { type: "term", data: "ls -la\r\n", ts: 1 });
    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(browser.sent).toEqual([]);
  });

  it("delivers a presence_sync from the window on activation", async () => {
    // The startup presence_sync is computed at THIS browser's join, so it
    // cannot carry a user who arrives while it is still starting up.
    const { router, connections, browser } = await pending();

    await router.broadcast(WORKER, {
      type: "presence_sync",
      users: [{ user_id: "a" }, { user_id: "b" }],
      config: {},
    });
    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(seen(browser, "presence_sync")).toEqual(["presence_sync"]);
  });

  it("delivers a presence_leave from the window on activation", async () => {
    // Worse than a missed sync: a delta, so dropping it keeps a ghost user.
    const { router, connections, browser } = await pending();

    await router.broadcast(WORKER, { type: "presence_leave", user_id: "departed" });
    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(seen(browser, "departed")).toEqual(["departed"]);
  });

  it("delivers a control_transfer from the window on activation", async () => {
    // Who is driving is a delta too, and nothing restates it.
    const { router, connections, browser } = await pending();

    await router.broadcast(WORKER, {
      type: "control_transfer",
      from_user_id: "a",
      to_user_id: "b",
      reason: "handover",
    });
    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(seen(browser, "control_transfer")).toEqual(["control_transfer"]);
  });

  it("does not replay a presence_update from the window", async () => {
    // Transient per-user state the next update supersedes.
    const { router, connections, browser } = await pending();

    await router.broadcast(WORKER, {
      type: "presence_update",
      user_id: "a",
      name: "A",
      color: "#fff",
      role: "viewer",
    });
    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(browser.sent).toEqual([]);
  });

  it("sends inspect frames straight to an activated browser", async () => {
    const { router, connections, browser } = await pending();
    await connections.activateBrowserBroadcasts(WORKER, browser);

    await router.broadcast(WORKER, httpReq("r1", "/api/users"));

    expect(seen(browser, "/api/users")).toEqual(["/api/users"]);
  });

  it("caps the buffer rather than growing it without bound", async () => {
    // A browser that never activates must not be able to grow this forever.
    const { hub, router, browser } = await pending();

    for (let index = 0; index < STARTUP_BUFFER_MAX_FRAMES + 10; index += 1) {
      await router.broadcast(WORKER, httpReq(`r${index}`, `/api/${index}`));
    }

    expect(hub.startupPendingFrames.get(browser)?.length).toBe(STARTUP_BUFFER_MAX_FRAMES);
  });

  it("drops the backlog when the browser disconnects", async () => {
    const { hub, router, connections, browser } = await pending();

    await router.broadcast(WORKER, httpReq("r1", "/api/users"));
    connections.cleanupBrowserDisconnect(WORKER, browser, false);

    expect(hub.startupPendingFrames.has(browser)).toBe(false);
    expect(hub.startupPendingBrowsers.has(browser)).toBe(false);
  });

  it("leaves a socket that cannot take its backlog pending", async () => {
    // Pending means the broadcast path skips it, which is the right resting
    // state for a connection that just failed a write.
    const { hub, router, connections, browser } = await pending();

    await router.broadcast(WORKER, httpReq("r1", "/api/users"));
    browser.failWrites();
    await connections.activateBrowserBroadcasts(WORKER, browser);

    expect(hub.startupPendingBrowsers.has(browser)).toBe(true);
    expect(hub.startupPendingFrames.has(browser)).toBe(false);
  });
});
