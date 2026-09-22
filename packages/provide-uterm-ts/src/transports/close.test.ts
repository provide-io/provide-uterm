//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { closeFromException, TransportClose, TransportClosedError, TransportConnectionError } from "./index.ts";

describe("the typed close", () => {
  it("is a connection error carrying the close", () => {
    const close = new TransportClose("local", { code: 1011, reason: "keepalive ping timeout" });

    const error = new TransportClosedError("Connection closed", close);

    // Still the old error type, so a caller catching that keeps working.
    expect(error).toBeInstanceOf(TransportConnectionError);
    expect(error.name).toBe("TransportClosedError");
    expect(error.close).toBe(close);
    expect(error.message).toBe("Connection closed (local close 1011 keepalive ping timeout)");
  });

  it("keeps the underlying failure as its cause", () => {
    const cause = new Error("socket gone");
    const error = new TransportClosedError("Connection lost", new TransportClose("unknown"), { cause });
    expect(error.cause).toBe(cause);
  });

  it.each([
    [new TransportClose("remote", { code: 1001, reason: "going away" }), "remote close 1001 going away"],
    [new TransportClose("remote"), "remote close"],
    [
      new TransportClose("unknown", { detail: "ConnectionResetError: reset" }),
      "unknown close (ConnectionResetError: reset)",
    ],
    [new TransportClose("local", { code: 1000 }), "local close 1000"],
    [new TransportClose("local", { code: 0 }), "local close 0"],
    [new TransportClose("unknown", { reason: "injected disconnect" }), "unknown close injected disconnect"],
    [
      new TransportClose("remote", { code: 1006, reason: "abnormal", detail: "received 1006" }),
      "remote close 1006 abnormal (received 1006)",
    ],
  ])("summarises %o as %s", (close, summary) => {
    expect(close.summary()).toBe(summary);
  });

  it("defaults to no code, reason or detail", () => {
    const close = new TransportClose("remote");
    expect(close.initiator).toBe("remote");
    expect(close.code).toBeUndefined();
    expect(close.reason).toBe("");
    expect(close.detail).toBe("");
  });
});

describe("closeFromException", () => {
  it("names the error type and message, attributing it to nobody by default", () => {
    const close = closeFromException(Object.assign(new Error("peer reset"), { name: "ConnectionResetError" }));
    expect(close.initiator).toBe("unknown");
    expect(close.detail).toBe("ConnectionResetError: peer reset");
    expect(close.code).toBeUndefined();
    expect(close.reason).toBe("");
  });

  it("attributes it to the side it is told", () => {
    expect(closeFromException(new TypeError("bad"), "remote")).toMatchObject({
      initiator: "remote",
      detail: "TypeError: bad",
    });
  });

  it("describes a thrown value that is not an error as it prints", () => {
    expect(closeFromException("gone").detail).toBe("gone");
  });
});
