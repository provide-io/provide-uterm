//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Egress target validation for outbound connector connections.
 *
 * Port of the Python module `provide.uterm.server.egress`.
 *
 * This decides where a hosted session may connect out to, so every case it
 * gets wrong is reachable by whoever can create a session.
 *
 * - Cloud-metadata addresses are always blocked. They are never a legitimate
 *   terminal target, and reaching one hands out cloud credentials.
 * - A wrapper is not a disguise: an IPv6 address can carry a reachable IPv4
 *   four ways — mapped, 6to4, NAT64 and the deprecated compatible form — and
 *   each is a way past a check that only looks at the outer address. In a
 *   NAT64 cluster `64:ff9b::169.254.169.254` really does reach the v4
 *   metadata service.
 * - A name that will not resolve fails closed, or a hostile — or merely
 *   broken — resolver turns the guard off.
 * - Private ranges are blocked only when asked: reaching internal hosts is
 *   what a connector is *for*, and the flag is the multi-tenant posture.
 */

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
  sixToFour,
} from "../pycompat/index.ts";

/** Raised when a target resolves to a forbidden address. */
export class EgressBlockedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EgressBlockedError";
  }
}

/** How a host name is turned into addresses. */
export type Resolver = (host: string) => Promise<string[]>;

/** Cloud-metadata services an outbound connection must never reach. */
export const METADATA_IPS: readonly string[] = ["100.100.100.200", "169.254.169.254", "fd00:ec2::254"];

/** The NAT64 well-known prefix (RFC 6052). */
export const NAT64_PREFIX = "64:ff9b::/96";

/**
 * How long a webhook host's resolution is reused.
 *
 * Short enough that a rebind is caught on the next miss, long enough that
 * consulting the webhook per message is not a resolver storm.
 */
export const EGRESS_DNS_TTL_S = 60.0;

/** Bound on one resolution, so a hostile resolver cannot hang a request. */
export const EGRESS_RESOLVE_TIMEOUT_S = 5.0;

const METADATA_ADDRESSES = METADATA_IPS.map((text) => ipAddress(text) as IpAddress);
const NAT64_NETWORK = ipNetwork(NAT64_PREFIX) as ReturnType<typeof ipNetwork> & object;

/**
 * host → [resolved at, addresses]
 *
 * Process-wide, as in the reference: the point is to survive across requests.
 */
const resolveCache = new Map<string, [number, string[]]>();

/** Forget every cached resolution. */
export function clearEgressCache(): void {
  resolveCache.clear();
}

/**
 * Strip the brackets and whitespace a peer address arrives with.
 *
 * Exported because the webhook validator needs exactly this: a WHATWG URL
 * reports an IPv6 host bracketed, and a bracketed string is not an address.
 */
export function cleanEgressHost(text: string): string {
  return text.trim().replace(/^\[|\]$/g, "");
}

/**
 * The IPv4 an IPv6 address carries, in any embedding form.
 *
 * The deprecated compatible form excludes `::` and `::1`, whose low bits are
 * 0 and 1: the ordinary IPv6 branches already cover them, and decoding would
 * turn `::` into `0.0.0.0` and lose the distinction.
 */
export function decodeEmbeddedIPv4(address: IpAddress): IpAddress | undefined {
  if (address.version !== 6) {
    return undefined;
  }
  const mapped = ipv4Mapped(address);
  if (mapped !== undefined) {
    return mapped;
  }
  const sixto = sixToFour(address);
  if (sixto !== undefined) {
    return sixto;
  }
  if (networkContains(NAT64_NETWORK, address)) {
    return { version: 4, packed: address.packed.slice(12) };
  }
  const highBytesAreZero = address.packed.slice(0, 12).every((byte) => byte === 0);
  const low = address.packed.slice(12);
  const lowValue =
    ((low[0] as number) << 24) | ((low[1] as number) << 16) | ((low[2] as number) << 8) | (low[3] as number);
  if (highBytesAreZero && lowValue !== 0 && lowValue !== 1) {
    return { version: 4, packed: low };
  }
  return undefined;
}

/** Whether two addresses are the same. */
function sameAddress(left: IpAddress, right: IpAddress): boolean {
  return left.version === right.version && left.packed.every((byte, index) => byte === right.packed[index]);
}

/**
 * What one address is, as far as any egress policy is concerned.
 *
 * `loopback` is split out from `internal` because it is the only case any
 * configuration key may re-open, and because it is re-opened by a *different*
 * key for a *different* reason than the private ranges are — see §2 of
 * `conformance/EGRESS_GUARD.md`. Every policy in this package decides over
 * these four values, so there is one classifier to be wrong rather than two
 * that can disagree.
 */
export type EgressAddressKind = "metadata" | "loopback" | "internal" | "public";

/**
 * Classify one address, wrapper and all.
 *
 * The wrapper is decoded first: a check applied to the outer address of
 * `64:ff9b::169.254.169.254` sees an unremarkable IPv6 address, and in a
 * NAT64 cluster that address reaches the v4 metadata service.
 */
export function classifyEgressAddress(address: IpAddress): EgressAddressKind {
  const subject = decodeEmbeddedIPv4(address) ?? address;
  if (METADATA_ADDRESSES.some((metadata) => sameAddress(metadata, subject))) {
    return "metadata";
  }
  if (isLoopback(subject)) {
    return "loopback";
  }
  // Link-local and unspecified are each *already* inside CPython's private
  // tables, so dropping either changes nothing today. They are kept because
  // the reference keeps them: the tables are CPython's to change, and this
  // list is what the rule actually means. Reserved and multicast are not
  // redundant — 100::/8 and ff00::/8 are outside the private list entirely.
  if (
    isPrivate(subject) ||
    isLinkLocal(subject) ||
    isMulticast(subject) ||
    isUnspecified(subject) ||
    isReserved(subject) ||
    isSharedAddressSpace(subject)
  ) {
    return "internal";
  }
  return "public";
}

/** RFC 6598 carrier-grade NAT space, `100.64.0.0/10`. */
function isSharedAddressSpace(address: IpAddress): boolean {
  // Checked here rather than added to `pycompat/ipaddress.ts`, because that
  // module is a deliberate mirror of CPython and CPython answers False:
  //
  //     >>> ipaddress.ip_address("100.64.0.1").is_private
  //     False
  //
  // Making the mirror lie would break the thing it exists to guarantee. So the
  // range is added by the *policy* that needs it, which is the layer entitled
  // to be stricter than the classifier it borrows. CGNAT carries real
  // infrastructure on carrier and container networks and is exactly what an
  // SSRF pivot wants; every port blocks it. See EGRESS_GUARD.md §1.
  if (address.version !== 4) {
    return false;
  }
  // 100.64.0.0/10 — the /10 puts the boundary inside the second octet, so
  // 100.63.255.255 and 100.128.0.0 are both outside it.
  const second = address.packed[1] ?? 0;
  return address.packed[0] === 100 && second >= 64 && second <= 127;
}

/**
 * Apply the metadata-always, private-when-asked policy to one address.
 *
 * Loopback lands in the `onPrivate` refusal rather than a case of its own:
 * `127.0.0.0/8` and `::1` are inside CPython's private tables, so the
 * reference refuses them under exactly that rule and with exactly that
 * message. The connector guard has no loopback key — the webhook one does.
 *
 * @throws {EgressBlockedError} With the caller's message for whichever rule
 *   refused it — the two call for different operator responses.
 */
function checkResolvedIp(
  address: IpAddress,
  options: { blockPrivate: boolean; onMetadata: string; onPrivate: string },
): void {
  const kind = classifyEgressAddress(address);
  if (kind === "metadata") {
    throw new EgressBlockedError(options.onMetadata);
  }
  if (options.blockPrivate && kind !== "public") {
    throw new EgressBlockedError(options.onPrivate);
  }
}

/**
 * Validate a peer address that has already been resolved.
 *
 * Used after a handshake completes but before any data flows: the address is
 * already a literal, so there is no second lookup and no rebinding window,
 * while the handshake itself still used the original hostname and so kept TLS
 * SNI and SSH host-key verification intact.
 *
 * @throws {EgressBlockedError} If the peer is a blocked target.
 */
export function assertIpAllowed(text: string, options: { blockPrivate: boolean }): void {
  const address = ipAddress(cleanEgressHost(text));
  if (address === undefined) {
    throw new TypeError(`not an IP address: ${text}`);
  }
  checkResolvedIp(address, {
    blockPrivate: options.blockPrivate,
    onMetadata: `connector peer '${text}' is a blocked metadata address`,
    onPrivate: `connector peer '${text}' is a blocked internal address`,
  });
}

/**
 * Validate a connector target, literal or name.
 *
 * Every address a name resolves to is checked: one good answer does not make
 * the name safe, because a rebinding reply puts the metadata address in the
 * same response.
 *
 * @throws {EgressBlockedError} If the target is blocked, or cannot be
 *   resolved at all.
 */
export async function assertConnectorTargetAllowed(
  host: string,
  options: { blockPrivate: boolean; resolve?: Resolver },
): Promise<void> {
  const clean = cleanEgressHost(host);
  const literal = ipAddress(clean);
  let addresses: string[];
  if (literal !== undefined) {
    // Nothing to look up, and a lookup would be a rebinding window.
    addresses = [ipToString(literal)];
  } else {
    const resolve = options.resolve ?? resolveThroughPlatform;
    try {
      addresses = await resolve(clean);
    } catch {
      // Distinct from an empty answer only in where it came from; both are
      // "no usable address", and both must fail closed.
      throw new EgressBlockedError(`could not resolve connector host '${host}'`);
    }
    // An empty answer must fail closed: the loop below would never run and
    // the host would be silently allowed.
    if (addresses.length === 0) {
      throw new EgressBlockedError(`could not resolve connector host '${host}'`);
    }
  }
  for (const text of addresses) {
    checkResolvedIp(ipAddress(text) as IpAddress, {
      blockPrivate: options.blockPrivate,
      // The operator configured the name; the address is only what it
      // answered with this time.
      onMetadata: `connector target '${host}' resolves to a blocked metadata address`,
      onPrivate: `connector target '${host}' resolves to a blocked internal address`,
    });
  }
}

/**
 * Validate a webhook URL's host.
 *
 * Metadata is never a legitimate webhook target; internal hosts *are*
 * allowed, because a policy engine may be internal and HTTPS with
 * certificate validation covers the rest.
 *
 * @throws {EgressBlockedError} If the host is metadata, or cannot be resolved.
 */
export async function assertWebhookTargetAllowed(
  url: string,
  options: { resolve?: Resolver; now?: () => number } = {},
): Promise<void> {
  let host: string;
  try {
    host = new URL(url).hostname;
  } catch {
    return;
  }
  if (host === "") {
    return;
  }
  const clean = cleanEgressHost(host);
  const literal = ipAddress(clean);
  let addresses: string[];
  if (literal !== undefined) {
    addresses = [ipToString(literal)];
  } else {
    try {
      addresses = await resolveCached(clean, options);
    } catch {
      throw new EgressBlockedError(`webhook target '${url}' could not be resolved`);
    }
    if (addresses.length === 0) {
      throw new EgressBlockedError(`webhook target '${url}' could not be resolved`);
    }
  }
  for (const text of addresses) {
    if (classifyEgressAddress(ipAddress(text) as IpAddress) === "metadata") {
      throw new EgressBlockedError(`webhook target '${url}' resolves to a blocked metadata address`);
    }
  }
}

/** Resolve, reusing a recent answer. A failure is not cached. */
async function resolveCached(host: string, options: { resolve?: Resolver; now?: () => number }): Promise<string[]> {
  const now = options.now ?? (() => Date.now() / 1000);
  const at = now();
  const cached = resolveCache.get(host);
  if (cached !== undefined && at - cached[0] < EGRESS_DNS_TTL_S) {
    return cached[1];
  }
  const resolve = options.resolve ?? resolveThroughPlatform;
  const addresses = await resolve(host);
  resolveCache.set(host, [at, addresses]);
  return addresses;
}

/**
 * Resolve through the platform.
 *
 * Exported so the webhook validator resolves the way everything else in this
 * module does. Two spellings of "ask the platform" is two places for one of
 * them to start answering differently — over IPv4 only, say, which would hide
 * a private AAAA record from the guard.
 */
export async function resolveThroughPlatform(host: string): Promise<string[]> {
  const { lookup } = await import("node:dns/promises");
  const answers = await lookup(host, { all: true });
  return answers.map((answer) => answer.address);
}
