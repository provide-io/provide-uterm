//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { inetAton, ipToString } from "../pycompat/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  ALLOW_PRIVATE_HOSTS,
  ALLOWED_CONNECTOR_TYPES,
  HIJACK_LEASE_REQUIRED_TOOLS,
  isAllowedConnector,
  isInternalHost,
  MAX_KEYSTROKE_BYTES,
  MAX_USER_PATTERN_LEN,
  type Role,
  requiredRole,
  roleAtLeast,
  roleRank,
  TOOL_REQUIRED_ROLES,
} from "./index.ts";

interface McpGolden {
  roles: Record<string, number>;
  role_ladder: Array<{ actual: string; minimum: Role; allowed: boolean }>;
  tool_roles: Record<string, Role>;
  unknown_tool: { role: null; error: string };
  hijack_lease_required: string[];
  allowed_connectors: string[];
  connectors: Record<string, boolean>;
  max_user_pattern_len: number;
  max_keystroke_bytes: number;
  allow_private_hosts: boolean;
  inet_aton: Array<{ value: string; address: string | null }>;
  hosts: Array<{ name: string; host: string; internal: boolean }>;
  patterns: Array<{ name: string; pattern: string | null; rejection: Record<string, unknown> | null }>;
  ids: Array<{ name: string; id: string; rejection: Record<string, unknown> | null }>;
}

const golden = loadGolden<McpGolden>("mcppolicy_golden.json");

describe("the role ladder", () => {
  it.each(Object.entries(golden.roles))("ranks %s", (role, rank) => {
    expect(roleRank(role)).toBe(rank);
  });

  it.each(golden.role_ladder)("$actual meets $minimum: $allowed", (record) => {
    expect(roleAtLeast(record.actual, record.minimum)).toBe(record.allowed);
  });

  it("ranks a role nobody defined below viewer", () => {
    // So an unrecognised role is never accidentally privileged — including
    // one that only differs in case.
    for (const role of ["", "root", "Admin", "superuser"]) {
      expect(roleRank(role)).toBe(-1);
      expect(roleAtLeast(role, "viewer")).toBe(false);
    }
  });

  it("is a total order", () => {
    expect(roleAtLeast("admin", "viewer")).toBe(true);
    expect(roleAtLeast("admin", "operator")).toBe(true);
    expect(roleAtLeast("operator", "admin")).toBe(false);
    expect(roleAtLeast("viewer", "operator")).toBe(false);
    expect(roleAtLeast("viewer", "viewer")).toBe(true);
  });
});

describe("which role each tool needs", () => {
  it("names every tool the reference names, at the same role", () => {
    // The single source of truth on both sides: a tool added to one and not
    // the other fails here.
    expect(Object.fromEntries(TOOL_REQUIRED_ROLES)).toEqual(golden.tool_roles);
  });

  it.each(Object.entries(golden.tool_roles))("%s needs %s", (tool, role) => {
    expect(requiredRole(tool)).toBe(role);
  });

  it("raises for a tool with no policy rather than guessing one", () => {
    // What stops a newly added tool from slipping through unguarded: a tool
    // with no entry must not run at all.
    expect(() => requiredRole("session_delete")).toThrow(golden.unknown_tool.error);
    expect(() => requiredRole("")).toThrow("No authorization policy registered");
  });

  it("puts every wide-blast-radius tool at admin", () => {
    // Spawning a process, disconnecting a worker, broadcasting input, taking
    // a session over.
    for (const tool of [
      "session_create",
      "worker_disconnect",
      "worker_input_mode",
      "fanout_send",
      "hijack_begin",
      "hijack_send",
      "gui_hijack_begin",
    ]) {
      expect(requiredRole(tool)).toBe("admin");
    }
  });

  it("keeps read-only inspection at viewer", () => {
    for (const tool of ["session_list", "session_status", "session_read", "server_health", "session_watch"]) {
      expect(requiredRole(tool)).toBe("viewer");
    }
  });

  it("needs a lease as well as a role for the tools that drive a screen", () => {
    // A role says who may ask; a lease says nobody else is currently driving.
    expect([...HIJACK_LEASE_REQUIRED_TOOLS].sort()).toEqual(golden.hijack_lease_required);
    for (const tool of HIJACK_LEASE_REQUIRED_TOOLS) {
      expect(TOOL_REQUIRED_ROLES.has(tool)).toBe(true);
    }
  });
});

describe("which connectors may be spawned", () => {
  it.each(Object.entries(golden.connectors))("%s: %s", (name, allowed) => {
    expect(isAllowedConnector(name)).toBe(allowed);
  });

  it("names the ones the reference names", () => {
    expect([...ALLOWED_CONNECTOR_TYPES].sort()).toEqual(golden.allowed_connectors);
  });

  it("matches exactly, not by case", () => {
    expect(isAllowedConnector("SHELL")).toBe(false);
    expect(isAllowedConnector("")).toBe(false);
  });
});

describe("reading an address the way a resolver reads it", () => {
  it.each(golden.inet_aton)("$value", (record) => {
    const parsed = inetAton(record.value);
    expect(parsed === undefined ? null : ipToString(parsed)).toBe(record.address);
  });

  it("reaches loopback by every form that reaches loopback", () => {
    // The whole reason this exists: an LLM that cannot write 127.0.0.1 can
    // write any of these, and a resolver takes them all.
    for (const value of ["2130706433", "0177.0.0.1", "0x7f.1", "127.1", "0x7f000001", "127.0.1"]) {
      expect(ipToString(inetAton(value) as ReturnType<typeof inetAton> & object)).toBe("127.0.0.1");
    }
  });

  it("refuses a genuine hostname, which is the resolver's business", () => {
    expect(inetAton("localhost")).toBeUndefined();
    expect(inetAton("bbs.example.com")).toBeUndefined();
  });

  it("allows trailing whitespace and refuses leading", () => {
    expect(inetAton("1.2.3.4 ")).toBeDefined();
    expect(inetAton("1.2.3.4\t")).toBeDefined();
    expect(inetAton(" 1.2.3.4")).toBeUndefined();
  });

  it("refuses a field that overflows its own place", () => {
    expect(inetAton("256.0.0.1")).toBeUndefined();
    expect(inetAton("1.2.3.256")).toBeUndefined();
    expect(inetAton("1.16777216")).toBeUndefined();
    expect(inetAton("1.2.65536")).toBeUndefined();
    expect(inetAton("0400.0.0.1")).toBeUndefined();
  });

  it("wraps a whole address that overflows rather than refusing it", () => {
    // Which is what the C function does, and why `999999999999` is an
    // address at all.
    expect(ipToString(inetAton("4294967296") as ReturnType<typeof inetAton> & object)).toBe("0.0.0.0");
    expect(ipToString(inetAton("999999999999") as ReturnType<typeof inetAton> & object)).toBe("212.165.15.255");
  });

  it("refuses an empty field anywhere", () => {
    for (const value of ["1.2.3.", ".1.2.3", "1..2.3", "", "."]) {
      expect(inetAton(value)).toBeUndefined();
    }
  });

  it("refuses a sign, an exponent, or trailing rubbish", () => {
    for (const value of ["-1", "+1", "1e3", "1.2.3.4x", "0x", "0xg", "1.2.3.4/24"]) {
      expect(inetAton(value)).toBeUndefined();
    }
  });
});

describe("where a session may be pointed", () => {
  it.each(golden.hosts)("$name", (record) => {
    expect(isInternalHost(record.host)).toBe(record.internal);
  });

  it("refuses loopback however it is written", () => {
    for (const host of ["127.0.0.1", "localhost", "LOCALHOST", "localhost.", "::1", "[::1]", "2130706433", "127.1"]) {
      expect(isInternalHost(host)).toBe(true);
    }
  });

  it("refuses the whole localhost subtree", () => {
    // RFC 6761 reserves it and requires it to resolve to loopback.
    expect(isInternalHost("api.localhost")).toBe(true);
    expect(isInternalHost("api.localhost.")).toBe(true);
    // But not a name that merely ends in those letters.
    expect(isInternalHost("notlocalhost")).toBe(false);
  });

  it("refuses cloud metadata by name and by address", () => {
    for (const host of ["metadata", "metadata.google.internal", "169.254.169.254", "2852039166"]) {
      expect(isInternalHost(host)).toBe(true);
    }
  });

  it("refuses private ranges by default and allows them on request", () => {
    // An operator who genuinely needs an internal target opts in; an LLM
    // does not get to.
    expect(ALLOW_PRIVATE_HOSTS).toBe(false);
    expect(ALLOW_PRIVATE_HOSTS).toBe(golden.allow_private_hosts);
    for (const host of ["10.0.0.5", "192.168.1.1", "fd00::1"]) {
      expect(isInternalHost(host)).toBe(true);
      expect(isInternalHost(host, true)).toBe(false);
    }
  });

  it("never allows loopback or link-local, even opted in", () => {
    // Opting in to private ranges is not opting in to the machine itself.
    for (const host of ["127.0.0.1", "localhost", "169.254.169.254", "fe80::1"]) {
      expect(isInternalHost(host, true)).toBe(true);
    }
  });

  it("lets a genuine hostname past, for the server to resolve", () => {
    // No DNS lookup happens here: rebinding and egress control are the
    // server's, and a lookup in an admission check would block it.
    for (const host of ["bbs.example.com", "", ".", "999999999999"]) {
      expect(isInternalHost(host)).toBe(false);
    }
  });

  it("allows a public address", () => {
    expect(isInternalHost("93.184.216.34")).toBe(false);
    expect(isInternalHost("1568399394")).toBe(false);
  });
});

describe("the limits an LLM is held to", () => {
  it("caps a caller-supplied pattern at the reference's length", () => {
    expect(MAX_USER_PATTERN_LEN).toBe(golden.max_user_pattern_len);
    const atCap = golden.patterns.find((entry) => entry.name === "a pattern at the length cap");
    const overCap = golden.patterns.find((entry) => entry.name === "a pattern over the length cap");
    expect(atCap?.rejection).toBeNull();
    expect(overCap?.rejection).toMatchObject({ success: false, error: "invalid_pattern" });
    expect((atCap?.pattern as string).length).toBe(MAX_USER_PATTERN_LEN);
  });

  it("caps a keystroke send at the reference's size", () => {
    expect(MAX_KEYSTROKE_BYTES).toBe(golden.max_keystroke_bytes);
  });

  it("refuses the patterns that would take exponential time", () => {
    // Recorded from the reference's own guard, which this port already has
    // in `hub/pattern-safety`.
    for (const name of ["a nested quantifier", "a quantified backreference"]) {
      expect(golden.patterns.find((entry) => entry.name === name)?.rejection).toMatchObject({
        error: "invalid_pattern",
      });
    }
  });

  it("refuses an id that would climb out of its path", () => {
    for (const name of ["an id with a slash in it", "an id with a dot segment", "an empty id"]) {
      expect(golden.ids.find((entry) => entry.name === name)?.rejection).toMatchObject({
        success: false,
        error: "invalid_id",
      });
    }
    expect(golden.ids.find((entry) => entry.name === "an ordinary id")?.rejection).toBeNull();
  });
});
