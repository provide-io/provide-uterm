//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A closed transport says who closed it, and the session keeps that answer.
 *
 * Port of the session half of `provide-uterm/tests/test_transport_close.py`.
 * Before this the reader swallowed every failure, so a client keepalive
 * timeout and a server closing the socket were indistinguishable.
 */

import { describe, expect, it } from "vitest";
import { TransportClose, TransportClosedError } from "../transports/index.ts";
import { type SessionTransport, TransportSession } from "./index.ts";

/**
 * Yields one chunk, then fails every later read with `error` — or, with no
 * error, blocks until closed and then fails with `onClose`.
 */
class EndsWith implements SessionTransport {
  receives = 0;
  #release: (() => void) | undefined;
  readonly error: unknown;
  readonly onClose: unknown;

  constructor(error: unknown, onClose: unknown = undefined) {
    this.error = error;
    this.onClose = onClose;
  }

  async connect(): Promise<void> {
    this.receives = 0;
  }

  async close(): Promise<void> {
    this.#release?.();
  }

  async send(): Promise<void> {}

  async receive(): Promise<string | undefined> {
    this.receives += 1;
    if (this.receives === 1) {
      return "hello";
    }
    if (this.error !== undefined) {
      throw this.error;
    }
    await new Promise<void>((resolve) => {
      this.#release = resolve;
    });
    if (this.onClose !== undefined) {
      throw this.onClose;
    }
    return undefined;
  }
}

/** Wait for the reader to notice the transport has gone. */
async function untilDisconnected(session: TransportSession): Promise<void> {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (!session.isConnected()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error("the reader never noticed the transport close");
}

const GOING_AWAY = new TransportClose("remote", { code: 1001, reason: "going away" });

describe("TransportSession.closeInfo", () => {
  it("is empty on a new session", () => {
    expect(new TransportSession({ transport: new EndsWith(undefined) }).closeInfo).toBeUndefined();
  });

  it("keeps the close the transport reported", async () => {
    const session = new TransportSession({
      transport: new EndsWith(new TransportClosedError("Connection closed", GOING_AWAY)),
    });

    await session.connect();
    await untilDisconnected(session);

    expect(session.closeInfo).toBe(GOING_AWAY);
  });

  it("makes an untyped drop an unknown close with its detail", async () => {
    const reset = Object.assign(new Error("peer reset"), { name: "ConnectionResetError" });
    const session = new TransportSession({ transport: new EndsWith(reset) });

    await session.connect();
    await untilDisconnected(session);

    expect(session.closeInfo).toMatchObject({ initiator: "unknown", detail: "ConnectionResetError: peer reset" });
  });

  it("records closing the session as a local close", async () => {
    const session = new TransportSession({ transport: new EndsWith(undefined) });
    await session.connect();

    await session.close();

    expect(session.closeInfo).toMatchObject({ initiator: "local", detail: "closed by client" });
  });

  it("records a local close even for a session that never connected", async () => {
    const session = new TransportSession({ transport: new EndsWith(undefined) });

    await session.close();

    expect(session.closeInfo?.initiator).toBe("local");
  });

  it("keeps the transport's close when the session is closed afterwards", async () => {
    const session = new TransportSession({
      transport: new EndsWith(new TransportClosedError("Connection closed", GOING_AWAY)),
    });
    await session.connect();
    await untilDisconnected(session);

    await session.close();

    expect(session.closeInfo).toBe(GOING_AWAY);
  });

  it("does not let the teardown it caused overwrite a local close", async () => {
    // Closing the transport fails the pending read; that failure is this
    // side's own close, not news from the far end.
    const teardown = new TransportClosedError("Connection closed", new TransportClose("remote"));
    const session = new TransportSession({ transport: new EndsWith(undefined, teardown) });
    await session.connect();
    await new Promise((resolve) => setTimeout(resolve, 1));

    await session.close();

    expect(session.closeInfo?.initiator).toBe("local");
  });

  it("forgets the last close on a fresh connect", async () => {
    const transport = new EndsWith(new TransportClosedError("Connection closed", GOING_AWAY));
    const session = new TransportSession({ transport });
    await session.connect();
    await untilDisconnected(session);

    await session.connect();

    expect(session.closeInfo).toBeUndefined();
    await untilDisconnected(session);
  });
});
