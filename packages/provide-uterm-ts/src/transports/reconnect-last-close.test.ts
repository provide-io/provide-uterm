//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A reconnecting session keeps the close that made it reconnect.
 *
 * Port of `provide-uterm-client/tests/transports/test_reconnect_last_close.py`.
 */

import { describe, expect, it } from "vitest";
import {
  isRetryableTransportError,
  reconnecting,
  TransportClose,
  TransportClosedError,
  TransportConnectionError,
} from "./index.ts";

const GOING_AWAY = new TransportClose("remote", { code: 1001, reason: "going away" });

/** A session that drops once on send, or reports a close it observed. */
class FakeSession {
  closeInfo: TransportClose | undefined;
  closed = false;
  drops: TransportClose | undefined;

  constructor(drops: TransportClose | undefined, observed: TransportClose | undefined = undefined) {
    this.drops = drops;
    this.closeInfo = observed;
  }

  async close(): Promise<void> {
    this.closed = true;
    // What a real session does: closing records this side's own close.
    this.closeInfo ??= new TransportClose("local", { detail: "closed by client" });
  }

  async send(): Promise<string> {
    const drops = this.drops;
    if (drops !== undefined) {
      this.drops = undefined;
      throw new TransportClosedError("Connection closed", drops);
    }
    return "sent";
  }
}

/** A connect function handing out `sessions` in turn. */
function handOut(sessions: FakeSession[]): () => Promise<FakeSession> {
  return async () => sessions.shift() as FakeSession;
}

const noSleep = async () => undefined;

describe("Reconnecting.lastClose", () => {
  it("is empty before anything has dropped", async () => {
    const proxy = reconnecting(handOut([new FakeSession(undefined)]), { sleep: noSleep });
    expect(await proxy.run((session) => session.send())).toBe("sent");
    expect(proxy.lastClose).toBeUndefined();
  });

  it("keeps the close that caused a reconnect", async () => {
    const proxy = reconnecting(handOut([new FakeSession(GOING_AWAY), new FakeSession(undefined)]), {
      sleep: noSleep,
    });

    expect(await proxy.run((session) => session.send())).toBe("sent");

    expect(proxy.lastClose).toBe(GOING_AWAY);
  });

  it("prefers the close the dead session observed, read before closing it", async () => {
    // The session saw the far end go before the operation's error said so;
    // reading after closing would instead find this side's own close.
    const observed = new TransportClose("remote", { code: 1006 });
    const dead = new FakeSession(GOING_AWAY, observed);
    const proxy = reconnecting(handOut([dead, new FakeSession(undefined)]), { sleep: noSleep });

    await proxy.run((session) => session.send());

    expect(proxy.lastClose).toBe(observed);
    expect(dead.closed).toBe(true);
  });

  it("keeps the raised close when the dead session observed none", async () => {
    // A session type without closeInfo at all still works.
    const bare = { close: async () => undefined };
    const proxy = reconnecting(async () => bare, { sleep: noSleep });
    let first = true;

    await proxy.run(async () => {
      if (first) {
        first = false;
        throw new TransportClosedError("Connection closed", GOING_AWAY);
      }
      return "ok";
    });

    expect(proxy.lastClose).toBe(GOING_AWAY);
  });

  it("does not record a retryable failure that is not a typed close", async () => {
    const bare = { close: async () => undefined };
    const proxy = reconnecting(async () => bare, { sleep: noSleep });
    let first = true;

    await proxy.run(async () => {
      if (first) {
        first = false;
        throw Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" });
      }
      return "ok";
    });

    expect(proxy.lastClose).toBeUndefined();
  });

  it("records the close even when the budget runs out", async () => {
    const proxy = reconnecting(handOut([new FakeSession(GOING_AWAY)]), {
      sleep: noSleep,
      policy: { maxRetries: 0 },
    });

    await expect(proxy.run((session) => session.send())).rejects.toThrow("reconnect retries exhausted");

    expect(proxy.lastClose).toBe(GOING_AWAY);
  });
});

describe("the port's own connection errors", () => {
  it("are retried, as the reference retries ConnectionError", () => {
    expect(isRetryableTransportError(new TransportConnectionError("Not connected"))).toBe(true);
    expect(isRetryableTransportError(new TransportClosedError("Connection closed", GOING_AWAY))).toBe(true);
  });
});
