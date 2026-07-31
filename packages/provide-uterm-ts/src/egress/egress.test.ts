//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it, vi } from "vitest";
import type { IpAddress } from "../pycompat/index.ts";
import { ipAddress, ipToString } from "../pycompat/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  assertConnectorTargetAllowed,
  assertIpAllowed,
  assertWebhookTargetAllowed,
  classifyEgressAddress,
  cleanEgressHost,
  clearEgressCache,
  decodeEmbeddedIPv4,
  EGRESS_DNS_TTL_S,
  EGRESS_RESOLVE_TIMEOUT_S,
  EgressBlockedError,
  METADATA_IPS,
  NAT64_PREFIX,
} from "./index.ts";

interface EgressGolden {
  literals: Array<{ input: string; permissive: string | null; strict: string | null }>;
  targets: Array<{ name: string; host: string; permissive: string | null; strict: string | null }>;
  webhooks: Array<{ name: string; url: string; blocked: string | null }>;
  malformed: {
    peer_error: string;
    peer_is_block: boolean;
    webhook_no_scheme: string | null;
    webhook_empty: string | null;
  };
  metadata_ips: string[];
  nat64_prefix: string;
  dns_ttl_s: number;
  resolve_timeout_s: number;
}

const golden = loadGolden<EgressGolden>("egress_golden.json");

/** What each recorded host resolves to. `undefined` means it will not. */
const RESOLUTIONS: Record<string, string[] | undefined> = {
  "example.org": ["93.184.216.34"],
  "policy.example.org": ["93.184.216.34"],
  "metadata.example": ["169.254.169.254"],
  "mixed.example": ["93.184.216.34", "169.254.169.254"],
  "internal.example": ["10.0.0.1"],
  "wrapped.example": ["64:ff9b::169.254.169.254"],
  "empty.example": [],
  "broken.example": undefined,
};

/** A resolver over the recorded table, counting how often it was asked. */
function resolver() {
  const calls: string[] = [];
  return {
    calls,
    resolve: async (host: string) => {
      calls.push(host);
      const addresses = RESOLUTIONS[host];
      if (addresses === undefined) {
        throw new Error(`cannot resolve ${host}`);
      }
      return addresses;
    },
  };
}

/** Run and return the block message, or nothing when allowed. */
function blocked(call: () => void): string | null {
  try {
    call();
  } catch (error) {
    return (error as Error).message;
  }
  return null;
}

/** Await and return the block message, or nothing when allowed. */
async function blockedAsync(call: () => Promise<void>): Promise<string | null> {
  try {
    await call();
  } catch (error) {
    return (error as Error).message;
  }
  return null;
}

describe("assertIpAllowed", () => {
  it.each(golden.literals)("$input while private is allowed", (record) => {
    // The permissive posture: reaching internal hosts is what a connector is
    // *for*, but a metadata address never is.
    expect(blocked(() => assertIpAllowed(record.input, { blockPrivate: false }))).toBe(record.permissive);
  });

  it.each(golden.literals)("$input while private is blocked", (record) => {
    expect(blocked(() => assertIpAllowed(record.input, { blockPrivate: true }))).toBe(record.strict);
  });

  it("always blocks the metadata services", () => {
    // Reaching one hands out cloud credentials, so the flag does not matter.
    for (const ip of golden.metadata_ips) {
      expect(blocked(() => assertIpAllowed(ip, { blockPrivate: false }))).toContain("metadata");
    }
    expect([...METADATA_IPS].sort()).toStrictEqual(golden.metadata_ips);
  });

  it("sees through every wrapper a metadata address can wear", () => {
    // Each of these is a way past a membership check that only looks at the
    // outer address. In a NAT64 cluster the third one really is reachable.
    for (const wrapped of [
      "::ffff:169.254.169.254",
      "2002:a9fe:a9fe::",
      "64:ff9b::169.254.169.254",
      "::169.254.169.254",
    ]) {
      const record = golden.literals.find((entry) => entry.input === wrapped);
      expect(record?.permissive).toContain("metadata");
      expect(blocked(() => assertIpAllowed(wrapped, { blockPrivate: false }))).toContain("metadata");
    }
  });

  it("sees through the same wrappers on an internal address", () => {
    for (const wrapped of ["::ffff:127.0.0.1", "2002:7f00:1::", "64:ff9b::127.0.0.1", "::127.0.0.1"]) {
      expect(blocked(() => assertIpAllowed(wrapped, { blockPrivate: true }))).toContain("internal");
      expect(blocked(() => assertIpAllowed(wrapped, { blockPrivate: false }))).toBeNull();
    }
  });

  it("strips brackets and whitespace before deciding", () => {
    // A peer address arrives bracketed from an IPv6 socket, and a stray space
    // would otherwise make it unparseable and the guard would throw something
    // other than a block.
    expect(blocked(() => assertIpAllowed("[169.254.169.254]", { blockPrivate: false }))).toContain("metadata");
    expect(blocked(() => assertIpAllowed(" 169.254.169.254 ", { blockPrivate: false }))).toContain("metadata");
  });

  it("quotes the address it refused", () => {
    const record = golden.literals.find((entry) => entry.input === "169.254.169.254");
    expect(record?.permissive).toContain("'169.254.169.254'");
  });

  it("distinguishes a metadata refusal from an internal one", () => {
    // The two call for different operator responses: one is an attack, the
    // other is a posture setting.
    const metadata = golden.literals.find((entry) => entry.input === "169.254.169.254");
    const internal = golden.literals.find((entry) => entry.input === "10.0.0.1");
    expect(metadata?.strict).toContain("metadata");
    expect(internal?.strict).toContain("internal");
  });

  it("blocks a reserved address that is not private", () => {
    // CPython's private list does not cover these, so the reserved check is
    // the only thing between them and a connector.
    for (const text of ["100:0:0:1::1", "200::1", "1000::1"]) {
      const record = golden.literals.find((entry) => entry.input === text);
      expect(record?.permissive).toBeNull();
      expect(record?.strict).toContain("internal");
      expect(blocked(() => assertIpAllowed(text, { blockPrivate: true }))).toContain("internal");
    }
  });

  it("blocks a multicast address that is not private", () => {
    const record = golden.literals.find((entry) => entry.input === "ff02::1");
    expect(record?.strict).toContain("internal");
  });

  it("lets a public address through either way", () => {
    const record = golden.literals.find((entry) => entry.input === "8.8.8.8");
    expect(record?.permissive).toBeNull();
    expect(record?.strict).toBeNull();
  });

  it("raises the shared error type", () => {
    expect(() => assertIpAllowed("169.254.169.254", { blockPrivate: false })).toThrow(EgressBlockedError);
    // The class name, not merely membership: a caller reading `.name` off a
    // caught error (logs, an error-shape contract) must see the real one.
    try {
      assertIpAllowed("169.254.169.254", { blockPrivate: false });
      expect.unreachable("expected assertIpAllowed to throw");
    } catch (error) {
      expect((error as Error).name).toBe("EgressBlockedError");
    }
  });

  it("fails on something that is not an address at all, and not as a block", () => {
    // A caller that hands this a hostname has a bug; reporting it as a block
    // would send them looking for a firewall rule instead.
    expect(golden.malformed.peer_is_block).toBe(false);
    expect(() => assertIpAllowed("not-an-address", { blockPrivate: false })).toThrow(TypeError);
    expect(() => assertIpAllowed("not-an-address", { blockPrivate: false })).not.toThrow(EgressBlockedError);
    // The exact message, not just the error class: a refusal that swapped its
    // wording for an empty string would still be "a TypeError" and this test
    // would not notice without pinning the text.
    expect(() => assertIpAllowed("not-an-address", { blockPrivate: false })).toThrow(
      "not an IP address: not-an-address",
    );
  });
});

describe("cleanEgressHost", () => {
  it("strips a bracket only where it actually wraps the address", () => {
    // A `[` or `]` elsewhere in the string is not the IPv6 wrapper and must
    // survive — otherwise the anchors guarding each side of the regex are
    // decoration rather than a rule.
    expect(cleanEgressHost("a[b]c")).toBe("a[b]c");
    expect(cleanEgressHost("[::1]")).toBe("::1");
  });
});

describe("classifyEgressAddress", () => {
  it("names each kind by its own word, not merely a truthy one", () => {
    expect(classifyEgressAddress(ipAddress("169.254.169.254") as IpAddress)).toBe("metadata");
    expect(classifyEgressAddress(ipAddress("127.0.0.1") as IpAddress)).toBe("loopback");
    expect(classifyEgressAddress(ipAddress("10.0.0.1") as IpAddress)).toBe("internal");
    expect(classifyEgressAddress(ipAddress("8.8.8.8") as IpAddress)).toBe("public");
  });

  it("does not let a version mismatch read as the same address", () => {
    // 169.254.169.254 (one of the three metadata addresses) packs to exactly
    // 4 bytes: A9:FE:A9:FE. A same-address check that dropped the version
    // comparison would call `.every()` on that shorter *metadata* array,
    // which only walks its own 4 indices — so a genuine v6 address whose
    // first 4 bytes happen to be A9:FE:A9:FE would compare equal without ever
    // looking at its remaining 12 bytes. a9fe:a9fe::1 is such an address: not
    // itself a wrapped form of the v4 metadata address (no wrapper decodes
    // it), yet reserved in its own right (a000::/3) rather than metadata.
    expect(classifyEgressAddress(ipAddress("a9fe:a9fe::1") as IpAddress)).toBe("internal");
  });

  it("classifies a genuinely public IPv6 address as public", () => {
    // Every other IPv6 case this suite exercises is loopback, link-local,
    // private or a wrapped metadata address; nothing else reaches the plain
    // "public" fallthrough for a v6 subject.
    expect(classifyEgressAddress(ipAddress("2001:4860:4860::8888") as IpAddress)).toBe("public");
  });

  it("draws the CGNAT boundary on the second octet alone", () => {
    // 100.64.0.0/10: the /10 puts the boundary inside the second octet, so an
    // address whose *first* octet is not 100 must never be caught by it, even
    // when the second octet is inside [64, 127].
    expect(classifyEgressAddress(ipAddress("100.64.0.0") as IpAddress)).toBe("internal");
    expect(classifyEgressAddress(ipAddress("100.127.255.255") as IpAddress)).toBe("internal");
    expect(classifyEgressAddress(ipAddress("100.63.255.255") as IpAddress)).toBe("public");
    expect(classifyEgressAddress(ipAddress("100.128.0.0") as IpAddress)).toBe("public");
    expect(classifyEgressAddress(ipAddress("101.64.0.1") as IpAddress)).toBe("public");
  });
});

describe("decodeEmbeddedIPv4", () => {
  /** The IPv4 inside a wrapper, as a string. */
  function decoded(text: string): string | undefined {
    const address = ipAddress(text);
    const embedded = address === undefined ? undefined : decodeEmbeddedIPv4(address);
    return embedded === undefined ? undefined : ipToString(embedded);
  }

  it("reads each wrapper", () => {
    expect(decoded("::ffff:169.254.169.254")).toBe("169.254.169.254");
    expect(decoded("2002:a9fe:a9fe::")).toBe("169.254.169.254");
    expect(decoded("64:ff9b::169.254.169.254")).toBe("169.254.169.254");
    expect(decoded("::169.254.169.254")).toBe("169.254.169.254");
  });

  it("leaves an ordinary IPv6 address alone", () => {
    expect(decoded("2001:db8::1")).toBeUndefined();
    expect(decoded("fe80::1")).toBeUndefined();
  });

  it("leaves an IPv4 address alone", () => {
    expect(decoded("169.254.169.254")).toBeUndefined();
  });

  it("does not read the unspecified or loopback addresses as compatible form", () => {
    // Their low bits are 0 and 1; the ordinary IPv6 branches already cover
    // them, and decoding would turn :: into 0.0.0.0 and lose the distinction.
    expect(decoded("::")).toBeUndefined();
    expect(decoded("::1")).toBeUndefined();
    expect(decoded("::0.0.0.1")).toBeUndefined();
  });

  it("uses the NAT64 prefix the reference uses", () => {
    expect(NAT64_PREFIX).toBe(golden.nat64_prefix);
  });
});

describe("assertConnectorTargetAllowed", () => {
  it.each(golden.targets)("$name while private is allowed", async (record) => {
    const { resolve } = resolver();
    expect(await blockedAsync(() => assertConnectorTargetAllowed(record.host, { blockPrivate: false, resolve }))).toBe(
      record.permissive,
    );
  });

  it.each(golden.targets)("$name while private is blocked", async (record) => {
    const { resolve } = resolver();
    expect(await blockedAsync(() => assertConnectorTargetAllowed(record.host, { blockPrivate: true, resolve }))).toBe(
      record.strict,
    );
  });

  it("checks every address a name resolves to", async () => {
    // One good address does not make the name safe — a rebinding answer puts
    // the metadata IP in the same reply.
    const record = golden.targets.find((entry) => entry.name === "one bad address among good ones");
    expect(record?.permissive).toContain("metadata");
  });

  it("fails closed when a name will not resolve", async () => {
    // A hostile or merely broken resolver would otherwise turn the guard off.
    const record = golden.targets.find((entry) => entry.name === "a name that will not resolve");
    expect(record?.permissive).toContain("could not resolve");
  });

  it("fails closed when a name resolves to nothing at all", async () => {
    // The loop over an empty list never runs, so without this the host is
    // silently allowed.
    const record = golden.targets.find((entry) => entry.name === "a name that resolves to nothing");
    expect(record?.permissive).toContain("could not resolve");
  });

  it("does not resolve a literal address", async () => {
    // There is nothing to look up, and a lookup would be a rebinding window.
    const { calls, resolve } = resolver();
    await blockedAsync(() => assertConnectorTargetAllowed("8.8.8.8", { blockPrivate: false, resolve }));
    expect(calls).toStrictEqual([]);
  });

  it("names the host rather than the address it resolved to", async () => {
    // The operator configured the name; the address is what it happened to
    // answer with this time.
    const record = golden.targets.find((entry) => entry.name === "a name that resolves to metadata");
    expect(record?.permissive).toContain("metadata.example");
  });

  it("resolves through the platform when given no resolver", async () => {
    // Every other case injects one; without this the real path could be
    // broken and the suite would not notice. `localhost` comes from the hosts
    // file, so it needs no network.
    await expect(assertConnectorTargetAllowed("localhost", { blockPrivate: false })).resolves.toBeUndefined();
    await expect(assertConnectorTargetAllowed("localhost", { blockPrivate: true })).rejects.toThrow(EgressBlockedError);
  });

  it("raises the shared error type", async () => {
    const { resolve } = resolver();
    await expect(assertConnectorTargetAllowed("metadata.example", { blockPrivate: false, resolve })).rejects.toThrow(
      EgressBlockedError,
    );
  });
});

describe("assertWebhookTargetAllowed", () => {
  it.each(golden.webhooks)("$name", async (record) => {
    clearEgressCache();
    const { resolve } = resolver();
    expect(await blockedAsync(() => assertWebhookTargetAllowed(record.url, { resolve }))).toBe(record.blocked);
  });

  it("allows an internal host", async () => {
    // A policy engine may be internal; HTTPS and certificate validation cover
    // the rest.
    const record = golden.webhooks.find((entry) => entry.name === "an internal literal, which is allowed");
    expect(record?.blocked).toBeNull();
  });

  it("still blocks metadata", async () => {
    const record = golden.webhooks.find((entry) => entry.name === "a metadata literal");
    expect(record?.blocked).toContain("metadata");
  });

  it("sees through a wrapper here too", async () => {
    const record = golden.webhooks.find((entry) => entry.name === "a wrapped metadata literal");
    expect(record?.blocked).toContain("metadata");
  });

  it("unwraps a bracketed IPv6 host", async () => {
    const record = golden.webhooks.find((entry) => entry.name === "a bracketed ipv6 literal");
    expect(record?.blocked).toContain("metadata");
  });

  it("allows a url with no host to check", async () => {
    // There is nothing to resolve, and refusing would break a caller that
    // never meant to reach the network.
    const record = golden.webhooks.find((entry) => entry.name === "a url with no host at all");
    expect(record?.blocked).toBeNull();
  });

  it("fails closed on a resolution failure", async () => {
    const record = golden.webhooks.find((entry) => entry.name === "a name that will not resolve");
    expect(record?.blocked).toContain("could not be resolved");
  });

  it("allows a url it cannot parse at all", async () => {
    // There is no host to check, and refusing would break a caller that never
    // meant to reach the network.
    expect(golden.malformed.webhook_no_scheme).toBeNull();
    expect(golden.malformed.webhook_empty).toBeNull();
    const { resolve } = resolver();
    expect(await blockedAsync(() => assertWebhookTargetAllowed("not a url", { resolve }))).toBeNull();
    expect(await blockedAsync(() => assertWebhookTargetAllowed("", { resolve }))).toBeNull();
  });

  it("resolves through the platform when given no resolver", async () => {
    // The webhook path has its own resolver call, behind the cache, and it
    // needs the same real-path check the connector guard gets.
    clearEgressCache();
    await expect(assertWebhookTargetAllowed("https://localhost/decide")).resolves.toBeUndefined();
  });

  it("caches a resolution for the recorded window", async () => {
    // The webhook is consulted on every message; without the cache that is a
    // resolver storm. The cache is process-wide, as in the reference, so the
    // test starts from a known state rather than whatever ran before it.
    clearEgressCache();
    const { calls, resolve } = resolver();
    let clock = 1000;
    const options = { resolve, now: () => clock };
    await assertWebhookTargetAllowed("https://policy.example.org/decide", options);
    await assertWebhookTargetAllowed("https://policy.example.org/decide", options);
    expect(calls).toStrictEqual(["policy.example.org"]);

    clock += EGRESS_DNS_TTL_S + 1;
    await assertWebhookTargetAllowed("https://policy.example.org/decide", options);
    expect(calls).toHaveLength(2);
  });

  it("uses the recorded window and timeout", () => {
    // Short enough that a rebind is caught on the next miss, bounded so a
    // hostile resolver cannot hang the request.
    expect(EGRESS_DNS_TTL_S).toBe(golden.dns_ttl_s);
    expect(EGRESS_RESOLVE_TIMEOUT_S).toBe(golden.resolve_timeout_s);
  });

  it("treats the window edge as expired, not still valid", async () => {
    // The comparison is a strict `<`: at exactly the TTL the cached entry must
    // be treated as stale, not as one tick still inside the window.
    clearEgressCache();
    const { calls, resolve } = resolver();
    let clock = 1000;
    const options = { resolve, now: () => clock };
    await assertWebhookTargetAllowed("https://policy.example.org/decide", options);
    clock += EGRESS_DNS_TTL_S; // exactly at the boundary, not past it
    await assertWebhookTargetAllowed("https://policy.example.org/decide", options);
    expect(calls).toHaveLength(2);
  });

  it("uses a real epoch-seconds clock when no now() is given", async () => {
    // Every other test in this file injects `now`, which never exercises the
    // default clock resolveCached falls back to. A default that forgot to
    // divide by 1000 — or that returned nothing at all — would make every
    // elapsed-time comparison against EGRESS_DNS_TTL_S wrong.
    vi.useFakeTimers();
    try {
      clearEgressCache();
      const { calls, resolve } = resolver();
      vi.setSystemTime(1_700_000_000_000);
      await assertWebhookTargetAllowed("https://policy.example.org/decide", { resolve });
      vi.setSystemTime(1_700_000_000_000 + 10_000); // +10s, well inside the 60s window
      await assertWebhookTargetAllowed("https://policy.example.org/decide", { resolve });
      expect(calls).toStrictEqual(["policy.example.org"]);

      vi.setSystemTime(1_700_000_000_000 + (EGRESS_DNS_TTL_S + 1) * 1000);
      await assertWebhookTargetAllowed("https://policy.example.org/decide", { resolve });
      expect(calls).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not cache a failure", async () => {
    // A resolver that recovers should not stay broken for the whole window.
    clearEgressCache();
    const { calls, resolve } = resolver();
    const options = { resolve, now: () => 1000 };
    await blockedAsync(() => assertWebhookTargetAllowed("https://broken.example/decide", options));
    await blockedAsync(() => assertWebhookTargetAllowed("https://broken.example/decide", options));
    expect(calls).toStrictEqual(["broken.example", "broken.example"]);
  });
});
