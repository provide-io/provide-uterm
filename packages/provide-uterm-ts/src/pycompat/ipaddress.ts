//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython's `ipaddress` dialect.
 *
 * Address parsing is a security boundary here, not a formatting detail: it
 * decides whether an SSH server may accept any credential, whether a
 * proxy-supplied header is trusted, and whether an outbound request may reach
 * a link-local metadata service. All three fail *open* if this is more
 * permissive than CPython.
 *
 * The cases that matter are the ones that look like an address and are not.
 * CPython rejects `0177.0.0.1` outright rather than reading it as octal,
 * rejects the bare integer form `inet_aton` accepts, and rejects a trailing
 * dot — while `::ffff:127.0.0.1` *is* loopback.
 */

/** A parsed address. */
export interface IpAddress {
  /** 4 or 6. */
  version: 4 | 6;
  /** Network byte order: 4 bytes for IPv4, 16 for IPv6. */
  packed: Uint8Array;
  /** The zone this address is scoped to, when it carries one. */
  scopeId?: string;
}

/** A parsed network. */
export interface IpNetwork {
  /** 4 or 6. */
  version: 4 | 6;
  /** The network address, in network byte order. */
  packed: Uint8Array;
  /** How many leading bits are fixed. */
  prefixLength: number;
}

/** Only ASCII digits count: CPython's parser is not Unicode-aware here. */
const ASCII_DIGITS = /^[0-9]+$/;

/** A run of hex digits, at most one hextet's worth. */
const HEXTET = /^[0-9a-fA-F]{1,4}$/;

/** Parse one dotted-decimal IPv4 address, or nothing. */
function parseIPv4(text: string): Uint8Array | undefined {
  const parts = text.split(".");
  if (parts.length !== 4) {
    return undefined;
  }
  const packed = new Uint8Array(4);
  for (let index = 0; index < 4; index += 1) {
    const part = parts[index] as string;
    // Leading zeros are rejected rather than read as octal — that ambiguity
    // is exactly what makes `0177.0.0.1` a bypass in parsers that guess. The
    // length bound is belt and braces: every four-digit run is over 255
    // anyway, so dropping it changes nothing observable.
    if (!ASCII_DIGITS.test(part) || (part.length > 1 && part.startsWith("0")) || part.length > 3) {
      return undefined;
    }
    const value = Number(part);
    if (value > 255) {
      return undefined;
    }
    packed[index] = value;
  }
  return packed;
}

/** Parse the hextets on one side of a `::`, or nothing. */
function parseHextets(text: string): number[] | undefined {
  if (text === "") {
    return [];
  }
  const parts = text.split(":");
  const values: number[] = [];
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index] as string;
    // A trailing IPv4 form is only legal as the last two hextets.
    if (part.includes(".")) {
      if (index !== parts.length - 1) {
        return undefined;
      }
      const embedded = parseIPv4(part);
      if (embedded === undefined) {
        return undefined;
      }
      values.push(((embedded[0] as number) << 8) | (embedded[1] as number));
      values.push(((embedded[2] as number) << 8) | (embedded[3] as number));
      continue;
    }
    if (!HEXTET.test(part)) {
      return undefined;
    }
    values.push(Number.parseInt(part, 16));
  }
  return values;
}

/** Pack eight hextets into sixteen bytes. */
function packHextets(hextets: readonly number[]): Uint8Array {
  const packed = new Uint8Array(16);
  for (let index = 0; index < 8; index += 1) {
    packed[index * 2] = ((hextets[index] as number) >> 8) & 0xff;
    packed[index * 2 + 1] = (hextets[index] as number) & 0xff;
  }
  return packed;
}

/** Parse one IPv6 address, or nothing. */
function parseIPv6(text: string): { packed: Uint8Array; scopeId?: string } | undefined {
  const marker = text.indexOf("%");
  const address = marker < 0 ? text : text.slice(0, marker);
  const scopeId = marker < 0 ? undefined : text.slice(marker + 1);
  if (scopeId !== undefined && (scopeId === "" || scopeId.includes("%"))) {
    return undefined;
  }

  const elision = address.indexOf("::");
  let hextets: number[];
  if (elision < 0) {
    const parsed = parseHextets(address);
    if (parsed === undefined || parsed.length !== 8) {
      return undefined;
    }
    hextets = parsed;
  } else {
    // Only one run may be elided, or the address is ambiguous. Nothing
    // observable rests on this alone — a second `::` leaves an empty part
    // that fails the hextet test — but the check says the intent, and says
    // it before the parse rather than by accident afterwards.
    if (address.indexOf("::", elision + 1) >= 0) {
      return undefined;
    }
    const head = parseHextets(address.slice(0, elision));
    const tail = parseHextets(address.slice(elision + 2));
    if (head === undefined || tail === undefined) {
      return undefined;
    }
    // The elision has to stand for at least one hextet: eight written out
    // leaves nothing for it to mean.
    const elided = 8 - head.length - tail.length;
    if (elided < 1) {
      return undefined;
    }
    hextets = [...head, ...Array<number>(elided).fill(0), ...tail];
  }

  const packed = packHextets(hextets);
  return scopeId === undefined ? { packed } : { packed, scopeId };
}

/**
 * Parse an address the way CPython's `ip_address` does.
 *
 * @returns The address, or `undefined` where CPython raises `ValueError`.
 */
export function ipAddress(text: string): IpAddress | undefined {
  const v4 = parseIPv4(text);
  if (v4 !== undefined) {
    return { version: 4, packed: v4 };
  }
  // Without a colon it was an IPv4 candidate and failed; there is nothing an
  // IPv6 parse could make of it. Purely an early exit: the hextet test
  // rejects the same input a moment later.
  if (!text.includes(":")) {
    return undefined;
  }
  const v6 = parseIPv6(text);
  if (v6 === undefined) {
    return undefined;
  }
  return v6.scopeId === undefined
    ? { version: 6, packed: v6.packed }
    : { version: 6, packed: v6.packed, scopeId: v6.scopeId };
}

/** Parse a CIDR network. */
export function ipNetwork(text: string): IpNetwork | undefined {
  const slash = text.lastIndexOf("/");
  // A bare address is not a network. The guard is not load-bearing on its
  // own — without a slash the prefix length is the whole string, which is not
  // a number — but it is the reason, and it does not depend on that accident.
  if (slash < 0) {
    return undefined;
  }
  const address = ipAddress(text.slice(0, slash));
  const lengthText = text.slice(slash + 1);
  if (address === undefined || !ASCII_DIGITS.test(lengthText)) {
    return undefined;
  }
  const prefixLength = Number(lengthText);
  if (prefixLength > address.packed.length * 8) {
    return undefined;
  }
  return { version: address.version, packed: address.packed, prefixLength };
}

/** Whether `address` falls inside `network`. */
export function networkContains(network: IpNetwork, address: IpAddress): boolean {
  // A v4 address is never inside a v6 network, however the bits line up —
  // otherwise an allowlist entry matches something it never named.
  if (network.version !== address.version) {
    return false;
  }
  const wholeBytes = network.prefixLength >> 3;
  for (let index = 0; index < wholeBytes; index += 1) {
    if (address.packed[index] !== network.packed[index]) {
      return false;
    }
  }
  const remainder = network.prefixLength & 7;
  if (remainder === 0) {
    return true;
  }
  // The remaining bits matter: rounding up to a byte would widen the range.
  const mask = 0xff << (8 - remainder);
  return ((address.packed[wholeBytes] as number) & mask) === ((network.packed[wholeBytes] as number) & mask);
}

/** Parse a table of networks once, at module load. */
function table(entries: readonly string[]): IpNetwork[] {
  return entries.map((entry) => ipNetwork(entry) as IpNetwork);
}

// CPython's own classification tables, transcribed rather than re-derived:
// they are the definition of these predicates, not a summary of the RFCs.
const V4_LOOPBACK = table(["127.0.0.0/8"]);
const V4_LINK_LOCAL = table(["169.254.0.0/16"]);
const V4_MULTICAST = table(["224.0.0.0/4"]);
const V4_RESERVED = table(["240.0.0.0/4"]);
const V4_PRIVATE = table([
  "0.0.0.0/8",
  "10.0.0.0/8",
  "127.0.0.0/8",
  "169.254.0.0/16",
  "172.16.0.0/12",
  "192.0.0.0/24",
  "192.0.0.170/31",
  "192.0.2.0/24",
  "192.168.0.0/16",
  "198.18.0.0/15",
  "198.51.100.0/24",
  "203.0.113.0/24",
  "240.0.0.0/4",
  "255.255.255.255/32",
]);
const V4_PRIVATE_EXCEPTIONS = table(["192.0.0.9/32", "192.0.0.10/32"]);

const V6_LINK_LOCAL = table(["fe80::/10"]);
const V6_MULTICAST = table(["ff00::/8"]);
const V6_RESERVED = table([
  "::/8",
  "100::/8",
  "200::/7",
  "400::/6",
  "800::/5",
  "1000::/4",
  "4000::/3",
  "6000::/3",
  "8000::/3",
  "a000::/3",
  "c000::/3",
  "e000::/4",
  "f000::/5",
  "f800::/6",
  "fe00::/9",
]);
const V6_PRIVATE = table([
  "::1/128",
  "::/128",
  "::ffff:0.0.0.0/96",
  "64:ff9b:1::/48",
  "100::/64",
  "2001::/23",
  "2001:db8::/32",
  "2002::/16",
  "3fff::/20",
  "fc00::/7",
  "fe80::/10",
]);
const V6_PRIVATE_EXCEPTIONS = table([
  "2001:1::1/128",
  "2001:1::2/128",
  "2001:3::/32",
  "2001:4:112::/48",
  "2001:20::/28",
  "2001:30::/28",
]);

/** The IPv4-mapped prefix, whose members answer as their IPv4 selves. */
const V6_MAPPED = ipNetwork("::ffff:0.0.0.0/96") as IpNetwork;

/** Whether `address` is in any of `networks`. */
function inAny(networks: readonly IpNetwork[], address: IpAddress): boolean {
  return networks.some((network) => networkContains(network, address));
}

/** Every byte zero apart from a final `value`. */
function isExactly(address: IpAddress, value: number): boolean {
  const last = address.packed.length - 1;
  for (let index = 0; index < last; index += 1) {
    if (address.packed[index] !== 0) {
      return false;
    }
  }
  return address.packed[last] === value;
}

/**
 * The IPv4 address an IPv4-mapped IPv6 address stands for.
 *
 * CPython answers the classification questions for the mapped address, which
 * is why `::ffff:127.0.0.1` is loopback — the case a hand-rolled check misses,
 * turning a "loopback only" rule into one an outside host can satisfy.
 */
export function ipv4Mapped(address: IpAddress): IpAddress | undefined {
  if (address.version !== 6 || !networkContains(V6_MAPPED, address)) {
    return undefined;
  }
  return { version: 4, packed: address.packed.slice(12) };
}

/**
 * Answer a question about an IPv4-mapped address as its IPv4 self.
 *
 * CPython routes *every* classification through the mapped address, not only
 * loopback — which is why `::ffff:127.0.0.1` is loopback and not reserved,
 * even though its high bytes sit inside the reserved `::/8`.
 */
function viaMapped(address: IpAddress, predicate: (address: IpAddress) => boolean): boolean | undefined {
  const mapped = ipv4Mapped(address);
  return mapped === undefined ? undefined : predicate(mapped);
}

/** The 6to4 prefix, whose next 32 bits are a reachable IPv4. */
const V6_SIXTOFOUR = ipNetwork("2002::/16") as IpNetwork;

/**
 * The IPv4 address a 6to4 address carries.
 *
 * A membership check that only looked at the IPv6 form would miss that
 * `2002:a9fe:a9fe::1` reaches the v4 metadata service.
 */
export function sixToFour(address: IpAddress): IpAddress | undefined {
  if (address.version !== 6 || !networkContains(V6_SIXTOFOUR, address)) {
    return undefined;
  }
  return { version: 4, packed: address.packed.slice(2, 6) };
}

/** Whether the address is a loopback address. */
export function isLoopback(address: IpAddress): boolean {
  return (
    viaMapped(address, isLoopback) ?? (address.version === 4 ? inAny(V4_LOOPBACK, address) : isExactly(address, 1))
  );
}

/** Whether the address is in a private block. */
export function isPrivate(address: IpAddress): boolean {
  const mapped = viaMapped(address, isPrivate);
  if (mapped !== undefined) {
    return mapped;
  }
  const networks = address.version === 4 ? V4_PRIVATE : V6_PRIVATE;
  const exceptions = address.version === 4 ? V4_PRIVATE_EXCEPTIONS : V6_PRIVATE_EXCEPTIONS;
  return inAny(networks, address) && !inAny(exceptions, address);
}

/** Whether the address is link-local. */
export function isLinkLocal(address: IpAddress): boolean {
  return viaMapped(address, isLinkLocal) ?? inAny(address.version === 4 ? V4_LINK_LOCAL : V6_LINK_LOCAL, address);
}

/** Whether the address is multicast. */
export function isMulticast(address: IpAddress): boolean {
  return viaMapped(address, isMulticast) ?? inAny(address.version === 4 ? V4_MULTICAST : V6_MULTICAST, address);
}

/** Whether the address is reserved. */
export function isReserved(address: IpAddress): boolean {
  return viaMapped(address, isReserved) ?? inAny(address.version === 4 ? V4_RESERVED : V6_RESERVED, address);
}

/** Whether the address is the unspecified address. */
export function isUnspecified(address: IpAddress): boolean {
  return viaMapped(address, isUnspecified) ?? isExactly(address, 0);
}

/** Compress the longest run of zero hextets, the leftmost winning a tie. */
function compress(hextets: readonly number[]): string {
  let bestStart = -1;
  let bestLength = 0;
  let runStart = -1;
  for (let index = 0; index <= hextets.length; index += 1) {
    if (index < hextets.length && hextets[index] === 0) {
      runStart = runStart < 0 ? index : runStart;
      continue;
    }
    if (runStart >= 0) {
      const length = index - runStart;
      // Strictly greater, so the leftmost of two equal runs is kept.
      if (length > bestLength) {
        bestStart = runStart;
        bestLength = length;
      }
      runStart = -1;
    }
  }
  const parts = hextets.map((hextet) => hextet.toString(16));
  // A single zero is written out — `::` standing for one hextet is no shorter.
  if (bestLength < 2) {
    return parts.join(":");
  }
  return `${parts.slice(0, bestStart).join(":")}::${parts.slice(bestStart + bestLength).join(":")}`;
}

/**
 * The address in CPython's normalised form.
 *
 * Comparisons and logs use this, so it has to agree: an address inside
 * `::ffff:0:0/96` keeps its dotted tail, while one merely carrying those bits
 * elsewhere does not.
 */
export function ipToString(address: IpAddress): string {
  if (address.version === 4) {
    return [...address.packed].join(".");
  }
  const mapped = ipv4Mapped(address);
  if (mapped !== undefined) {
    return `::ffff:${ipToString(mapped)}`;
  }
  const hextets: number[] = [];
  for (let index = 0; index < 8; index += 1) {
    hextets.push(((address.packed[index * 2] as number) << 8) | (address.packed[index * 2 + 1] as number));
  }
  const base = compress(hextets);
  return address.scopeId === undefined ? base : `${base}%${address.scopeId}`;
}

/** The network in CPython's normalised form. */
export function networkToString(network: IpNetwork): string {
  return `${ipToString({ version: network.version, packed: network.packed })}/${network.prefixLength}`;
}

/**
 * The C resolver's `inet_aton`, which takes far more than a dotted quad.
 *
 * A blocklist that only understands `127.0.0.1` is trivially bypassed:
 * `2130706433`, `0177.0.0.1`, `0x7f.1` and `127.1` all reach loopback through
 * any resolver that uses this, which is every one that matters. Anything this
 * accepts has to be classified before it is allowed out.
 *
 * The grammar it actually implements, recorded rather than assumed: one to
 * four dot-separated fields; each field decimal, octal with a leading zero, or
 * hexadecimal with a leading `0x`; no sign; the last field holding whatever
 * bytes the earlier ones did not, and truncated to thirty-two bits rather than
 * refused when it overflows. Trailing whitespace is allowed and leading
 * whitespace is not.
 *
 * @returns The address, or nothing for anything it refuses — which includes
 *   every genuine hostname, and those are the resolver's business.
 */
export function inetAton(text: string): IpAddress | undefined {
  // Trailing whitespace only. A leading space is refused, which is why this
  // cannot simply trim.
  const trimmed = text.replace(/\s+$/, "");
  if (trimmed === "") {
    return undefined;
  }
  const fields = trimmed.split(".");
  if (fields.length > 4) {
    return undefined;
  }
  const values: number[] = [];
  for (const field of fields) {
    const value = parseCNumber(field);
    if (value === undefined) {
      return undefined;
    }
    values.push(value);
  }

  // Every field but the last holds one byte; the last holds the rest.
  const leading = values.slice(0, -1);
  if (leading.some((value) => value > 0xff)) {
    return undefined;
  }
  const last = values[values.length - 1] as number;
  const remainingBytes = 4 - leading.length;
  // Refused when it does not fit its own field — except the whole-address
  // form, which wraps.
  if (remainingBytes < 4 && last > 2 ** (8 * remainingBytes) - 1) {
    return undefined;
  }

  // Not reduced modulo 2^32 first: the per-byte extraction below already
  // discards everything above thirty-two bits, which is the same wrap.
  let packed = last;
  for (const [index, value] of leading.entries()) {
    packed += value * 2 ** (8 * (3 - index));
  }
  return ipAddress([24, 16, 8, 0].map((shift) => Math.floor(packed / 2 ** shift) % 256).join("."));
}

/** One `strtoul`-style field: hexadecimal, octal or decimal, and unsigned. */
function parseCNumber(field: string): number | undefined {
  if (/^0[xX][0-9a-fA-F]+$/.test(field)) {
    return Number.parseInt(field.slice(2), 16);
  }
  if (/^0[0-7]*$/.test(field)) {
    return Number.parseInt(field, 8);
  }
  if (/^[1-9][0-9]*$/.test(field)) {
    return Number.parseInt(field, 10);
  }
  return undefined;
}
