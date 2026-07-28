//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type IpAddress,
  ipAddress,
  ipNetwork,
  ipToString,
  ipv4Mapped,
  isLinkLocal,
  isLoopback,
  isMulticast,
  isPrivate,
  isReserved,
  isUnspecified,
  networkContains,
  networkToString,
} from "./ipaddress.ts";

interface IpGolden {
  addresses: Array<{
    input: string;
    valid: boolean;
    version?: 4 | 6;
    normalized?: string;
    packed?: number[];
    loopback?: boolean;
    private?: boolean;
    link_local?: boolean;
    multicast?: boolean;
    reserved?: boolean;
    unspecified?: boolean;
  }>;
  networks: Array<{
    name: string;
    network: string;
    normalized: string;
    members: Array<{ address: string; contains: boolean }>;
  }>;
}

const golden = loadGolden<IpGolden>("ipaddress_golden.json");

const valid = golden.addresses.filter((record) => record.valid);
const invalid = golden.addresses.filter((record) => !record.valid);

/** Parse a recorded address, failing loudly rather than silently skipping. */
function parse(text: string): IpAddress {
  const address = ipAddress(text);
  if (address === undefined) {
    throw new Error(`expected ${text} to parse`);
  }
  return address;
}

describe("parsing", () => {
  it.each(valid)("accepts $input", (record) => {
    const address = parse(record.input);
    expect(address.version).toBe(record.version);
    expect([...address.packed]).toStrictEqual(record.packed);
  });

  it.each(invalid)("rejects $input", (record) => {
    // Every one of these fails open if accepted: a rejected bind address
    // becomes a permissive SSH server, a rejected header becomes a trusted
    // proxy, a rejected host becomes a reachable metadata service.
    expect(ipAddress(record.input)).toBeUndefined();
  });

  it("rejects leading zeros rather than reading them as octal", () => {
    // `0177.0.0.1` is 127.0.0.1 to a parser that guesses at octal, and a
    // different host to one that does not. CPython refuses to guess.
    expect(invalid.map((record) => record.input)).toContain("0177.0.0.1");
    expect(invalid.map((record) => record.input)).toContain("010.0.0.1");
  });

  it("rejects the bare integer form", () => {
    // `inet_aton` accepts it; CPython does not, and a loopback check that
    // did would be bypassed by 2130706433.
    expect(ipAddress("2130706433")).toBeUndefined();
  });

  it("rejects surrounding whitespace and brackets", () => {
    for (const text of [" 127.0.0.1", "127.0.0.1 ", "[::1]"]) {
      expect(ipAddress(text)).toBeUndefined();
    }
  });

  it("rejects a prefix length on an address", () => {
    expect(ipAddress("127.0.0.1/8")).toBeUndefined();
  });

  it("rejects digits that are not ASCII", () => {
    // A full-width or Arabic-Indic digit is a digit to a Unicode-aware
    // parser and not to CPython.
    expect(ipAddress("12７.0.0.1")).toBeUndefined();
    expect(ipAddress("127.0.0.٠")).toBeUndefined();
  });

  it("rejects an elision that stands for nothing", () => {
    // Eight hextets are already written out, so `::` has no work to do.
    expect(ipAddress("1:2:3:4:5:6:7::8")).toBeUndefined();
  });

  it("rejects too few hextets without an elision", () => {
    expect(ipAddress("1:2:3")).toBeUndefined();
  });

  it("rejects an embedded IPv4 form anywhere but the tail", () => {
    expect(ipAddress("::127.0.0.1:ffff")).toBeUndefined();
  });

  it("rejects a malformed IPv4 tail", () => {
    // The octal bypass must be refused here too, or it comes back through
    // the IPv6 door.
    for (const text of ["::1.2.3", "::256.0.0.1", "::0177.0.0.1"]) {
      expect(ipAddress(text)).toBeUndefined();
    }
  });

  it("rejects an empty zone", () => {
    expect(ipAddress("fe80::1%")).toBeUndefined();
  });

  it("rejects more than one elision", () => {
    for (const text of [":::1", "::1::"]) {
      expect(ipAddress(text)).toBeUndefined();
    }
  });

  it("keeps a zone identifier", () => {
    const record = valid.find((entry) => entry.input === "fe80::1%eth0");
    expect(record).toBeDefined();
    expect(parse("fe80::1%eth0").scopeId).toBe("eth0");
  });

  it("reads an embedded IPv4 tail into the low bytes", () => {
    expect([...parse("::ffff:127.0.0.1").packed].slice(12)).toStrictEqual([127, 0, 0, 1]);
  });
});

describe("normalising", () => {
  it.each(valid)("$input", (record) => {
    expect(ipToString(parse(record.input))).toBe(record.normalized);
  });

  it("keeps the dotted tail only for a genuinely mapped address", () => {
    // `::ffff:0:127.0.0.1` has those bits somewhere else entirely, so it is
    // not the IPv4 address it resembles.
    expect(ipToString(parse("::ffff:127.0.0.1"))).toBe("::ffff:127.0.0.1");
    expect(ipToString(parse("::ffff:0:127.0.0.1"))).toBe("::ffff:0:7f00:1");
  });

  it("writes a single zero hextet out", () => {
    // `::` standing for one hextet is no shorter, and CPython does not use
    // it there — a form that did would not compare equal to CPython's.
    expect(ipToString(parse("1:2:3:4:5:6:0:8"))).toBe("1:2:3:4:5:6:0:8");
  });

  it("elides the leftmost of two equal runs", () => {
    expect(ipToString(parse("1:0:0:2:0:0:3:4"))).toBe("1::2:0:0:3:4");
  });

  it("compresses the longest run of zeros", () => {
    expect(ipToString(parse("0:0:0:0:0:0:0:1"))).toBe("::1");
  });

  it("carries the zone through", () => {
    expect(ipToString(parse("fe80::1%eth0"))).toBe("fe80::1%eth0");
  });
});

describe("classification", () => {
  const predicates: Array<[keyof IpGolden["addresses"][number], (address: IpAddress) => boolean]> = [
    ["loopback", isLoopback],
    ["private", isPrivate],
    ["link_local", isLinkLocal],
    ["multicast", isMulticast],
    ["reserved", isReserved],
    ["unspecified", isUnspecified],
  ];

  it.each(valid)("$input", (record) => {
    const address = parse(record.input);
    for (const [flag, predicate] of predicates) {
      expect({ [flag]: predicate(address) }).toStrictEqual({ [flag]: record[flag] });
    }
  });

  it("treats an IPv4-mapped loopback as loopback", () => {
    // This is the case a hand-rolled check misses, and missing it turns a
    // "loopback only" rule into one an outside host can satisfy.
    const record = valid.find((entry) => entry.input === "::ffff:127.0.0.1");
    expect(record?.loopback).toBe(true);
    expect(isLoopback(parse("::ffff:127.0.0.1"))).toBe(true);
  });

  it("does not treat the whole 127.0.0.0/8 block as one address", () => {
    expect(isLoopback(parse("127.255.255.254"))).toBe(true);
    expect(isLoopback(parse("126.255.255.255"))).toBe(false);
    expect(isLoopback(parse("128.0.0.1"))).toBe(false);
  });

  it("does not treat the all-interfaces bind as loopback", () => {
    // The single most consequential case: it is where an "accept anything"
    // server would be listening.
    expect(isLoopback(parse("0.0.0.0"))).toBe(false);
    expect(isLoopback(parse("::"))).toBe(false);
    expect(isUnspecified(parse("0.0.0.0"))).toBe(true);
  });

  it("bounds the private ranges at their real edges", () => {
    expect(isPrivate(parse("172.16.0.1"))).toBe(true);
    expect(isPrivate(parse("172.15.255.255"))).toBe(false);
    expect(isPrivate(parse("172.31.255.255"))).toBe(true);
    expect(isPrivate(parse("172.32.0.1"))).toBe(false);
  });

  it("bounds link-local at its real edge", () => {
    expect(isLinkLocal(parse("169.254.169.254"))).toBe(true);
    expect(isLinkLocal(parse("169.253.255.255"))).toBe(false);
  });

  it("does not treat site-local as link-local", () => {
    // `fec0::/10` is deprecated site-local, not link-local; conflating them
    // widens whatever the link-local rule guards.
    expect(isLinkLocal(parse("fec0::1"))).toBe(false);
    expect(isLinkLocal(parse("fe80::1"))).toBe(true);
    expect(isLinkLocal(parse("febf::1"))).toBe(true);
  });

  it("carves the exceptions back out of a private block", () => {
    // 192.0.0.0/24 is private except for two hosts, and 2001::/23 except for
    // several blocks. Ignoring the exceptions makes a public address look
    // private, which is the wrong way round for an egress guard.
    expect(isPrivate(parse("192.0.0.8"))).toBe(true);
    expect(isPrivate(parse("192.0.0.9"))).toBe(false);
    expect(isPrivate(parse("192.0.0.10"))).toBe(false);
    expect(isPrivate(parse("192.0.0.11"))).toBe(true);
    expect(isPrivate(parse("2001:1::1"))).toBe(false);
    expect(isPrivate(parse("2001:1::3"))).toBe(true);
  });

  it("treats the carrier-grade NAT block as public", () => {
    // 100.64.0.0/10 is not private to CPython, and the codebase's metadata
    // guard relies on that.
    expect(isPrivate(parse("100.100.100.200"))).toBe(false);
  });

  it("exposes the mapped address itself", () => {
    expect(ipv4Mapped(parse("::ffff:8.8.8.8"))).toStrictEqual(parse("8.8.8.8"));
    expect(ipv4Mapped(parse("::ffff:0:127.0.0.1"))).toBeUndefined();
    expect(ipv4Mapped(parse("127.0.0.1"))).toBeUndefined();
  });
});

describe("networks", () => {
  it.each(golden.networks)("$name", (record) => {
    const network = ipNetwork(record.network);
    expect(network).toBeDefined();
    expect(networkToString(network as NonNullable<typeof network>)).toBe(record.normalized);
    for (const member of record.members) {
      expect({
        [member.address]: networkContains(network as NonNullable<typeof network>, parse(member.address)),
      }).toStrictEqual({ [member.address]: member.contains });
    }
  });

  it("does not match across versions", () => {
    // A v4 address inside a v6 network would let an allowlist entry match
    // something it never named.
    const record = golden.networks.find((entry) => entry.name === "a v4 network across versions");
    expect(record?.members.find((member) => member.address === "::1")?.contains).toBe(false);
  });

  it("respects a prefix that is not a whole number of bytes", () => {
    // 64:ff9b::/96 is the case the egress guard uses, and an implementation
    // that rounds to a byte boundary would match a wider range.
    const network = ipNetwork("64:ff9b::/96");
    expect(networkContains(network as NonNullable<typeof network>, parse("64:ff9b::1"))).toBe(true);
    expect(networkContains(network as NonNullable<typeof network>, parse("64:ff9b:1::1"))).toBe(false);
  });

  it("rejects a network without a prefix length", () => {
    expect(ipNetwork("127.0.0.1")).toBeUndefined();
  });

  it("rejects a prefix length that is too long for the family", () => {
    expect(ipNetwork("127.0.0.0/33")).toBeUndefined();
    expect(ipNetwork("::/129")).toBeUndefined();
    expect(ipNetwork("::/128")).toBeDefined();
  });

  it("rejects a prefix length that is not a number", () => {
    expect(ipNetwork("127.0.0.0/eight")).toBeUndefined();
  });
});
