//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  assertAuthenticationConfigured,
  ConnectionLimiter,
  InsecureHostKeyError,
  isLoopbackBind,
  PermissiveAuthError,
  validatePassword,
  validatePublicKey,
  verifyKeyPermissions,
} from "./index.ts";

interface SshPolicyGolden {
  loopback: Array<{ host: string; loopback: boolean }>;
  key_permissions: Array<{ name: string; mode: number; mode_repr: string; error: string | null }>;
  foreign_owner: { message: string; owner_uid: number; current_uid: number; path: string };
  start_guard: Array<{ name: string; refused: boolean; message: string | null }>;
  per_ip_limit: {
    limit: number;
    events: Array<{ peer: [string, number] | null; rejected: boolean; counts: Record<string, number> }>;
    unknown_peer_rejected: boolean;
  };
  validators: {
    begin_auth: boolean;
    password_supported: boolean;
    public_key_supported: boolean;
    permissive_password: boolean;
    permissive_public_key: boolean;
    strict_password_right: boolean;
    strict_password_wrong: boolean;
    strict_key_right: boolean;
    strict_key_wrong: boolean;
  };
  expected_mode: number;
  expected_mode_repr: string;
  default_max_connections_per_ip: number;
}

const golden = loadGolden<SshPolicyGolden>("sshpolicy_golden.json");

/** The options for a recorded start case, in the port's spelling. */
function startOptions(name: string) {
  const cases: Record<string, Parameters<typeof assertAuthenticationConfigured>[0]> = {
    "no validators on a public bind": { host: "0.0.0.0" },
    "no validators on loopback": { host: "127.0.0.1" },
    "no validators on localhost": { host: "localhost" },
    "a password validator on a public bind": { host: "0.0.0.0", hasPasswordValidator: true },
    "a key validator on a public bind": { host: "0.0.0.0", hasPublicKeyValidator: true },
    "an explicit opt-in on a public bind": { host: "0.0.0.0", allowUnauthenticated: true },
    "no validators on a private range": { host: "10.0.0.1" },
  };
  return cases[name] as Parameters<typeof assertAuthenticationConfigured>[0];
}

describe("isLoopbackBind", () => {
  it.each(golden.loopback)("$host", (record) => {
    // This one predicate decides whether an "accept any credential" server is
    // allowed to start. Widening it puts one on a public interface.
    expect(isLoopbackBind(record.host)).toBe(record.loopback);
  });

  it("accepts the name as well as the address", () => {
    expect(isLoopbackBind("localhost")).toBe(true);
    expect(isLoopbackBind("127.0.0.1")).toBe(true);
    expect(isLoopbackBind("::1")).toBe(true);
  });

  it("matches the name exactly", () => {
    // `LOCALHOST` and `localhost.localdomain` are not the literal the
    // reference compares against, and it does not resolve names.
    expect(isLoopbackBind("LOCALHOST")).toBe(false);
    expect(isLoopbackBind("localhost.localdomain")).toBe(false);
  });

  it("rejects the all-interfaces bind", () => {
    // The case the whole guard exists for.
    expect(isLoopbackBind("0.0.0.0")).toBe(false);
    expect(isLoopbackBind("::")).toBe(false);
  });

  it("rejects an empty host rather than treating it as local", () => {
    expect(isLoopbackBind("")).toBe(false);
  });

  it("rejects a private address that is not loopback", () => {
    // A LAN address is still reachable by everyone on the LAN.
    expect(isLoopbackBind("10.0.0.1")).toBe(false);
    expect(isLoopbackBind("192.168.1.10")).toBe(false);
  });

  it("does not fall for an address that only looks like loopback", () => {
    for (const host of ["0177.0.0.1", "2130706433", "127.0.0.1."]) {
      expect(isLoopbackBind(host)).toBe(false);
    }
  });

  it("counts an IPv4-mapped loopback", () => {
    const record = golden.loopback.find((entry) => entry.host === "::ffff:127.0.0.1");
    expect(record?.loopback).toBe(true);
  });
});

describe("verifyKeyPermissions", () => {
  it.each(golden.key_permissions)("$name", (record) => {
    // A private key that anyone can read is not a secret, and loading it
    // anyway would be silent. The uid is the recorded placeholder on both
    // sides of the comparison, so this says "owned by the caller" without
    // depending on who the caller is.
    const owner = golden.foreign_owner.owner_uid;
    const check = () => verifyKeyPermissions(golden.foreign_owner.path, { mode: record.mode, uid: owner }, owner);
    if (record.error === null) {
      expect(check).not.toThrow();
    } else {
      expect(check).toThrow(record.error);
    }
  });

  it("accepts only the one safe mode", () => {
    const accepted = golden.key_permissions.filter((record) => record.error === null);
    expect(accepted).toHaveLength(1);
    expect(accepted[0]?.mode).toBe(golden.expected_mode);
  });

  it("refuses a mode that is merely stricter", () => {
    // 0400 cannot be written, so the server could not rotate the key; the
    // reference insists on exactly 0600 rather than "no wider than".
    const record = golden.key_permissions.find((entry) => entry.mode === 0o400);
    expect(record?.error).not.toBeNull();
  });

  it("reports the mode it found in octal", () => {
    const record = golden.key_permissions.find((entry) => entry.mode === 0o644);
    expect(record?.error).toContain("0o644");
    expect(record?.error).toContain(golden.expected_mode_repr);
  });

  it("names the file it refused", () => {
    const owner = golden.foreign_owner.owner_uid;
    expect(() => verifyKeyPermissions(golden.foreign_owner.path, { mode: 0o644, uid: owner }, owner)).toThrow(
      golden.foreign_owner.path,
    );
  });

  it("refuses a key owned by somebody else", () => {
    // Same bytes, different owner: it is their key, and it may be a key they
    // can still read. Both uids come from the corpus — they are placeholders
    // the reference was driven with, not the uid of whoever is running this.
    expect(() =>
      verifyKeyPermissions(
        golden.foreign_owner.path,
        { mode: 0o600, uid: golden.foreign_owner.owner_uid },
        golden.foreign_owner.current_uid,
      ),
    ).toThrow(golden.foreign_owner.message);
  });

  it("skips the ownership check where there are no uids", () => {
    // The reference skips it when the platform has no getuid; refusing every
    // key there would make the server unstartable.
    expect(() =>
      verifyKeyPermissions(golden.foreign_owner.path, { mode: 0o600, uid: golden.foreign_owner.owner_uid }),
    ).not.toThrow();
  });

  it("raises the shared error type", () => {
    expect(() => verifyKeyPermissions("/keys/k", { mode: 0o644, uid: 1 }, 1)).toThrow(InsecureHostKeyError);
  });
});

describe("assertAuthenticationConfigured", () => {
  it.each(golden.start_guard)("$name", (record) => {
    const check = () => assertAuthenticationConfigured(startOptions(record.name));
    if (record.refused) {
      expect(check).toThrow(record.message as string);
    } else {
      expect(check).not.toThrow();
    }
  });

  it("refuses only when nothing authenticates and nothing opted in", () => {
    // Any one of the three is enough: a validator, a loopback bind, or the
    // explicit opt-in.
    expect(golden.start_guard.filter((record) => record.refused).map((record) => record.name)).toStrictEqual([
      "no validators on a public bind",
      "no validators on a private range",
    ]);
  });

  it("does not treat a LAN bind as safe", () => {
    // A private range is not loopback: everyone on the LAN can reach it.
    const record = golden.start_guard.find((entry) => entry.name === "no validators on a private range");
    expect(record?.refused).toBe(true);
  });

  it("says what the operator can do about it", () => {
    const record = golden.start_guard.find((entry) => entry.refused);
    expect(record?.message).toContain("credentials_validator");
    expect(record?.message).toContain("loopback");
    expect(record?.message).toContain("allow_unauthenticated");
  });

  it("raises the shared error type", () => {
    expect(() => assertAuthenticationConfigured({ host: "0.0.0.0" })).toThrow(PermissiveAuthError);
  });
});

describe("ConnectionLimiter", () => {
  it("follows the recorded accept and reject sequence", () => {
    const limiter = new ConnectionLimiter(golden.per_ip_limit.limit);
    const admitted = [];
    for (const event of golden.per_ip_limit.events) {
      if (event.peer === null) {
        continue;
      }
      const slot = limiter.admit(event.peer);
      admitted.push({ rejected: slot === undefined, counts: limiter.counts() });
    }
    expect(admitted).toStrictEqual(
      golden.per_ip_limit.events
        .filter((event) => event.peer !== null)
        .map((event) => ({ rejected: event.rejected, counts: event.counts })),
    );
  });

  it("counts each address separately", () => {
    // One noisy client must not lock everybody else out.
    const busy = golden.per_ip_limit.events.find((event) => event.peer?.[0] === "10.0.0.2");
    expect(busy?.rejected).toBe(false);
  });

  it("gives the slot back when the connection ends", () => {
    // Without this a client eventually locks itself — and everyone behind the
    // same NAT — out of the server for good.
    const limiter = new ConnectionLimiter(1);
    const first = limiter.admit(["10.0.0.1", 1]);
    expect(first).toBeDefined();
    expect(limiter.admit(["10.0.0.1", 2])).toBeUndefined();
    first?.release();
    expect(limiter.admit(["10.0.0.1", 3])).toBeDefined();
  });

  it("forgets an address once it has no connections", () => {
    // Otherwise the table grows without bound for the life of the process.
    const limiter = new ConnectionLimiter(2);
    limiter.admit(["10.0.0.1", 1])?.release();
    expect(limiter.counts()).toStrictEqual(golden.per_ip_limit.events.at(-1)?.counts);
  });

  it("does not credit a rejected connection when it ends", () => {
    // A rejected connection still closes, and crediting it would let the next
    // one through — which is the whole limit, undone.
    const limiter = new ConnectionLimiter(1);
    limiter.admit(["10.0.0.1", 1]);
    expect(limiter.admit(["10.0.0.1", 2])).toBeUndefined();
    expect(limiter.counts()).toStrictEqual({ "10.0.0.1": 1 });
    expect(limiter.admit(["10.0.0.1", 3])).toBeUndefined();
  });

  it("does not give the same slot back twice", () => {
    const limiter = new ConnectionLimiter(2);
    limiter.admit(["10.0.0.1", 1]);
    const second = limiter.admit(["10.0.0.1", 2]);
    second?.release();
    second?.release();
    expect(limiter.counts()).toStrictEqual({ "10.0.0.1": 1 });
  });

  it("releases the address the connection was counted under", () => {
    // Not the last one admitted: two connections from different addresses
    // overlap constantly, and the reference keeps the peer per connection.
    const limiter = new ConnectionLimiter(2);
    const first = limiter.admit(["10.0.0.1", 1]);
    limiter.admit(["10.0.0.2", 2]);
    first?.release();
    expect(limiter.counts()).toStrictEqual({ "10.0.0.2": 1 });
  });

  it("buckets a connection whose peer is unknown", () => {
    const limiter = new ConnectionLimiter(golden.per_ip_limit.limit);
    expect(limiter.admit(undefined) === undefined).toBe(golden.per_ip_limit.unknown_peer_rejected);
    expect(limiter.counts()).toStrictEqual({ unknown: 1 });
  });

  it("limits the unknown bucket too", () => {
    const limiter = new ConnectionLimiter(1);
    expect(limiter.admit(undefined)).toBeDefined();
    expect(limiter.admit(undefined)).toBeUndefined();
  });

  it("defaults to the reference limit", () => {
    const limiter = new ConnectionLimiter();
    for (let index = 0; index < golden.default_max_connections_per_ip; index += 1) {
      expect(limiter.admit(["10.0.0.1", index])).toBeDefined();
    }
    expect(limiter.admit(["10.0.0.1", 99])).toBeUndefined();
  });
});

describe("the validators", () => {
  it("accepts anything when none is configured", () => {
    // Legitimate for a gateway that authenticates at the session layer, which
    // is exactly why the start guard exists.
    expect(validatePassword("anyone", "anything")).toBe(golden.validators.permissive_password);
    expect(validatePublicKey("anyone", undefined)).toBe(golden.validators.permissive_public_key);
  });

  it("defers to a configured password validator", () => {
    const validator = (_user: string, password: string) => password === "letmein"; // pragma: allowlist secret
    expect(validatePassword("anyone", "letmein", validator)).toBe(golden.validators.strict_password_right);
    expect(validatePassword("anyone", "hunter2", validator)).toBe(golden.validators.strict_password_wrong);
  });

  it("defers to a configured key validator", () => {
    const validator = (user: string) => user === "operator";
    expect(validatePublicKey("operator", undefined, validator)).toBe(golden.validators.strict_key_right);
    expect(validatePublicKey("intruder", undefined, validator)).toBe(golden.validators.strict_key_wrong);
  });
});
