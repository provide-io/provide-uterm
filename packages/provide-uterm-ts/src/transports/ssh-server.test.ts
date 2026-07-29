//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  connectionAllowed,
  DEFAULT_MAX_CONNECTIONS_PER_IP,
  HostKeyPermissionError,
  type IpConnectionCounts,
  isLoopbackBind,
  noteConnectionClosed,
  noteConnectionOpened,
  REQUIRED_HOST_KEY_MODE,
  sshServerMayStart,
  verifyHostKeyPermissions,
} from "./index.ts";

interface SshServerGolden {
  default_max_connections_per_ip: number;
  required_key_mode: number;
  loopback: Array<{ name: string; host: string; loopback: boolean }>;
  admission: Array<{
    name: string;
    has_password_validator: boolean;
    has_key_validator: boolean;
    allow_unauthenticated: boolean;
    host: string;
    allowed: boolean;
  }>;
  key_permissions: Array<{ name: string; mode: number; error: string | null }>;
  connections: Array<{ limit: number; existing: number; allowed: boolean }>;
}

const golden = loadGolden<SshServerGolden>("sshserver_golden.json");

describe("whether a bind reaches only this machine", () => {
  it.each(golden.loopback)("$name", (record) => {
    expect(isLoopbackBind(record.host)).toBe(record.loopback);
  });

  it("knows loopback written out and by name", () => {
    for (const host of ["127.0.0.1", "127.0.0.53", "localhost", "::1"]) {
      expect(isLoopbackBind(host)).toBe(true);
    }
  });

  it("does not know a name in capitals, and fails closed for it", () => {
    // The reference matches `localhost` case-sensitively. `LOCALHOST` is
    // therefore treated as routable and has to authenticate — the safe
    // direction to be wrong in, which is why it is left alone.
    expect(isLoopbackBind("LOCALHOST")).toBe(false);
    expect(
      sshServerMayStart({
        hasPasswordValidator: false,
        hasPublicKeyValidator: false,
        allowUnauthenticated: false,
        host: "LOCALHOST",
      }),
    ).toBe(false);
  });

  it("does not treat every address as loopback", () => {
    // The mistake that would matter: `0.0.0.0` binds everywhere.
    for (const host of ["0.0.0.0", "::", "10.0.0.5", "93.184.216.34"]) {
      expect(isLoopbackBind(host)).toBe(false);
    }
  });

  it("does not resolve a name to decide", () => {
    // A lookup in a start-up check would block it, and a name that resolves
    // to loopback today may not tomorrow.
    for (const host of ["ssh.example.com", "api.localhost", "notlocalhost", ""]) {
      expect(isLoopbackBind(host)).toBe(false);
    }
  });
});

describe("whether a server may start", () => {
  it.each(golden.admission)("$name", (record) => {
    expect(
      sshServerMayStart({
        hasPasswordValidator: record.has_password_validator,
        hasPublicKeyValidator: record.has_key_validator,
        allowUnauthenticated: record.allow_unauthenticated,
        host: record.host,
      }),
    ).toBe(record.allowed);
  });

  it("refuses the one combination where nothing checks anything", () => {
    // No validator of either kind means every password and every key is
    // accepted, so the bind address would be the only thing between the world
    // and a shell.
    expect(
      sshServerMayStart({
        hasPasswordValidator: false,
        hasPublicKeyValidator: false,
        allowUnauthenticated: false,
        host: "0.0.0.0",
      }),
    ).toBe(false);
  });

  it("takes either validator on its own", () => {
    // A server that checks passwords but accepts any key is the caller's
    // decision to make.
    for (const [password, key] of [
      [true, false],
      [false, true],
      [true, true],
    ] as const) {
      expect(
        sshServerMayStart({
          hasPasswordValidator: password,
          hasPublicKeyValidator: key,
          allowUnauthenticated: false,
          host: "93.184.216.34",
        }),
      ).toBe(true);
    }
  });

  it("takes no validators at all on loopback", () => {
    // Which is the case this exists for: a gateway authenticating at the
    // session layer above.
    for (const host of ["127.0.0.1", "localhost", "::1"]) {
      expect(
        sshServerMayStart({
          hasPasswordValidator: false,
          hasPublicKeyValidator: false,
          allowUnauthenticated: false,
          host,
        }),
      ).toBe(true);
    }
  });

  it("takes the opt-in, because somebody had to write it down", () => {
    expect(
      sshServerMayStart({
        hasPasswordValidator: false,
        hasPublicKeyValidator: false,
        allowUnauthenticated: true,
        host: "0.0.0.0",
      }),
    ).toBe(true);
  });

  it("refuses an empty bind, which is not loopback", () => {
    expect(
      sshServerMayStart({
        hasPasswordValidator: false,
        hasPublicKeyValidator: false,
        allowUnauthenticated: false,
        host: "",
      }),
    ).toBe(false);
  });
});

describe("whether a host key is private enough to load", () => {
  it.each(golden.key_permissions)("$name", (record) => {
    const run = () => verifyHostKeyPermissions("/k/ssh_host_key", record.mode, 1000, 1000);
    if (record.error === null) {
      expect(run).not.toThrow();
      return;
    }
    expect(run).toThrow(record.error);
  });

  it("refuses a key anybody else can read", () => {
    // A key another account can read is a key another account can impersonate
    // this server with.
    for (const mode of [0o640, 0o644, 0o666, 0o604]) {
      expect(() => verifyHostKeyPermissions("/k", mode, 1000, 1000)).toThrow(HostKeyPermissionError);
    }
  });

  it("refuses a stricter key too, because the match is exact", () => {
    // The reference compares the mode rather than bounding it: a key this
    // server cannot write is as much a surprise as one everybody can read.
    expect(() => verifyHostKeyPermissions("/k", 0o400, 1000, 1000)).toThrow("insecure mode 0o400");
    expect(() => verifyHostKeyPermissions("/k", 0o000, 1000, 1000)).toThrow("insecure mode 0o0");
    expect(REQUIRED_HOST_KEY_MODE).toBe(golden.required_key_mode);
  });

  it("refuses a key belonging to somebody else", () => {
    expect(() => verifyHostKeyPermissions("/k", 0o600, 0, 1000)).toThrow(
      "refusing to load SSH host key owned by uid 0 (current uid 1000): /k",
    );
  });

  it("takes a key it owns", () => {
    expect(() => verifyHostKeyPermissions("/k", 0o600, 1000, 1000)).not.toThrow();
  });

  it("skips the owner check where there are no uids", () => {
    // A platform without them cannot answer the question, and refusing every
    // key there would mean no server at all.
    expect(() => verifyHostKeyPermissions("/k", 0o600, 0, undefined)).not.toThrow();
  });

  it("checks the mode before the owner", () => {
    // So a key that is both wrong reports the problem an operator can act on
    // first.
    expect(() => verifyHostKeyPermissions("/k", 0o644, 0, 1000)).toThrow("insecure mode");
  });
});

describe("how many connections one address may hold", () => {
  it.each(golden.connections)("limit $limit with $existing already", (record) => {
    const counts: IpConnectionCounts = new Map(record.existing > 0 ? [["203.0.113.7", record.existing]] : []);
    expect(connectionAllowed(counts, "203.0.113.7", record.limit)).toBe(record.allowed);
  });

  it("counts at the limit, not past it", () => {
    const counts: IpConnectionCounts = new Map([["a", 5]]);
    expect(connectionAllowed(counts, "a", 5)).toBe(false);
    expect(connectionAllowed(counts, "a", 6)).toBe(true);
  });

  it("lets an address nobody has seen through", () => {
    expect(connectionAllowed(new Map(), "a", 1)).toBe(true);
  });

  it("counts each address on its own", () => {
    // So one host cannot take the server's whole capacity by itself.
    const counts: IpConnectionCounts = new Map([["a", 5]]);
    expect(connectionAllowed(counts, "b", 5)).toBe(true);
  });

  it("forgets an address once its last connection goes", () => {
    // A server that has seen many addresses should not keep one entry per
    // address it has ever met.
    const counts: IpConnectionCounts = new Map();
    noteConnectionOpened(counts, "a");
    noteConnectionOpened(counts, "a");
    expect(counts.get("a")).toBe(2);
    noteConnectionClosed(counts, "a");
    expect(counts.get("a")).toBe(1);
    noteConnectionClosed(counts, "a");
    expect(counts.has("a")).toBe(false);
  });

  it("forgets an address closed more often than it opened", () => {
    const counts: IpConnectionCounts = new Map();
    noteConnectionClosed(counts, "a");
    expect(counts.has("a")).toBe(false);
  });

  it("holds as many at once as the reference does", () => {
    expect(DEFAULT_MAX_CONNECTIONS_PER_IP).toBe(golden.default_max_connections_per_ip);
  });
});
