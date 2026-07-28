//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type AttachedSocket, browserRole, encodeAttachment, socketKind, socketWorkerId } from "./index.ts";

interface Described {
  kind: "value" | "mapping" | "object";
  value?: unknown;
  data?: Record<string, unknown>;
  role?: string;
}

interface AttachGolden {
  types: Array<{ name: string; attachment: Described; result: string }>;
  roles: Array<{ name: string; attachment: Described; mode: string; result: string }>;
  worker_ids: Array<{ name: string; attachment: Described; result: string }>;
  default_worker_id: string;
  unreadable_type: string;
  unreadable_role: string;
  unreadable_worker_id: string;
  open_mode_role: string;
  dev_mode_role: string;
}

const golden = loadGolden<AttachGolden>("cfattach_golden.json");

/** The attachment the corpus described. */
function rebuild(described: Described): unknown {
  if (described.kind === "mapping") {
    // Answers like a mapping, which is one of the shapes the runtime hands
    // back.
    return { get: (key: string) => described.data?.[key] };
  }
  if (described.kind === "object") {
    return { role: described.role };
  }
  return described.value;
}

/** A connection carrying an attachment. */
function socket(attachment: unknown): AttachedSocket {
  return { deserializeAttachment: () => attachment };
}

/** A connection that cannot say what it carries. */
function unreadable(): AttachedSocket {
  return {
    deserializeAttachment(): unknown {
      throw new Error("attachment unreadable");
    },
  };
}

describe("what kind of connection this is", () => {
  it.each(golden.types)("$name", (record) => {
    expect(socketKind(socket(rebuild(record.attachment)))).toBe(record.result);
  });

  it("reads the first field of the attachment", () => {
    expect(socketKind(socket("worker:admin:w1"))).toBe("worker");
    expect(socketKind(socket("raw"))).toBe("raw");
  });

  it("reads a mapping or an object that names one", () => {
    // The runtime hands the attachment back in more than one shape depending
    // on how it was stored.
    expect(socketKind(socket({ get: (key: string) => (key === "role" ? "worker" : undefined) }))).toBe("worker");
    expect(socketKind(socket({ role: "raw" }))).toBe("raw");
  });

  it("prefers what a mapping says over a property of the same name", () => {
    // The runtime may hand back something that answers both ways. The
    // reference asks the mapping first, and the two disagreeing is exactly
    // when it matters which.
    const both = { get: (key: string) => (key === "role" ? "worker" : undefined), role: "raw" };
    expect(socketKind(socket(both))).toBe("worker");
  });

  it("falls through to the property when the mapping says nothing", () => {
    const partial = { get: () => undefined, role: "raw" };
    expect(socketKind(socket(partial))).toBe("raw");
  });

  it("treats anything it does not recognise as a browser", () => {
    // The overwhelming majority are, and a connection mistaken for a worker
    // would be handed the session's output stream.
    for (const name of ["an unknown type", "an unknown type with fields", "a number", "nothing", "nothing at all"]) {
      expect(golden.types.find((entry) => entry.name === name)?.result).toBe("browser");
    }
  });

  it("treats a connection that cannot answer as a browser", () => {
    expect(socketKind(unreadable())).toBe(golden.unreadable_type);
  });
});

describe("what a browser connection may do", () => {
  it.each(golden.roles)("$name", (record) => {
    expect(browserRole(socket(rebuild(record.attachment)), { mode: record.mode })).toBe(record.result);
  });

  it("reads the middle field of three", () => {
    expect(browserRole(socket("browser:admin:w1"), { mode: "jwt" })).toBe("admin");
    expect(browserRole(socket("browser:operator:w1"), { mode: "jwt" })).toBe("operator");
    expect(browserRole(socket("browser:viewer:w1"), { mode: "jwt" })).toBe("viewer");
  });

  it("keeps the role readable when the session id contains a colon", () => {
    // Split any further and the role would read as "admin:w1", fail the
    // membership test, and silently demote an administrator to a viewer.
    expect(golden.roles.find((entry) => entry.name === "a session id containing a colon")?.result).toBe("admin");
    expect(browserRole(socket("browser:admin:w1:extra"), { mode: "jwt" })).toBe("admin");
  });

  it("reads a role with no session after it", () => {
    expect(browserRole(socket("browser:admin"), { mode: "jwt" })).toBe("admin");
  });

  it("fails closed on a role it cannot read", () => {
    // The post-hibernation case: the instance attribute set at connect time
    // does not survive eviction, so the attachment is all there is. A
    // connection whose role cannot be recovered is a viewer, not whatever it
    // was before.
    for (const name of [
      "an unknown role",
      "an empty role",
      "no role field at all",
      "an attachment that is not a string",
      "nothing at all",
      "nothing",
    ]) {
      expect(golden.roles.find((entry) => entry.name === name)?.result).toBe("viewer");
    }
    expect(browserRole(unreadable(), { mode: "jwt" })).toBe(golden.unreadable_role);
  });

  it("accepts only the three roles it knows", () => {
    // Anything else in that field is not a role, and treating it as one would
    // admit whatever an attachment happened to contain.
    expect(browserRole(socket("browser:root:w1"), { mode: "jwt" })).toBe("viewer");
    expect(browserRole(socket("browser:superuser:w1"), { mode: "jwt" })).toBe("viewer");
  });

  it("grants admin under the open modes the Worker no longer allows", () => {
    // The configuration refuses anything but jwt at startup, so this is
    // unreachable in a Worker that started at all. Kept because the function
    // can be called with any mode, and pinned so the fail-closed default is
    // known to be conditional rather than accidental.
    expect(browserRole(unreadable(), { mode: "none" })).toBe(golden.open_mode_role);
    expect(browserRole(unreadable(), { mode: "dev" })).toBe(golden.dev_mode_role);
    expect(golden.open_mode_role).toBe("admin");
  });
});

describe("which session a connection belongs to", () => {
  it.each(golden.worker_ids)("$name", (record) => {
    expect(socketWorkerId(socket(rebuild(record.attachment)), golden.default_worker_id)).toBe(record.result);
  });

  it("keeps a session id that contains a colon", () => {
    expect(socketWorkerId(socket("browser:admin:w1:extra"), "fallback")).toBe("w1:extra");
  });

  it("falls back to the object's own session", () => {
    // Right for a connection that predates the field, and for one whose
    // attachment cannot be read.
    expect(socketWorkerId(socket("browser:admin"), "w-default")).toBe("w-default");
    expect(socketWorkerId(socket("browser:admin:"), "w-default")).toBe("w-default");
    expect(socketWorkerId(unreadable(), "w-default")).toBe(golden.unreadable_worker_id);
  });
});

describe("writing an attachment", () => {
  it("produces one the readers understand", () => {
    // The round trip is the point: what is written at connect time is all
    // that survives eviction.
    const attachment = encodeAttachment("browser", "operator", "w1");
    expect(socketKind(socket(attachment))).toBe("browser");
    expect(browserRole(socket(attachment), { mode: "jwt" })).toBe("operator");
    expect(socketWorkerId(socket(attachment), "fallback")).toBe("w1");
  });

  it("survives a session id with a colon in it", () => {
    const attachment = encodeAttachment("browser", "admin", "w1:extra");
    expect(browserRole(socket(attachment), { mode: "jwt" })).toBe("admin");
    expect(socketWorkerId(socket(attachment), "fallback")).toBe("w1:extra");
  });
});
