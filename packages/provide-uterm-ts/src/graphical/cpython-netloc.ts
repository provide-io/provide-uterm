//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * How CPython's `urlsplit` reads a netloc, and what it refuses outright.
 *
 * Endpoint grammars in {@link ./targets.ts} are specified against the Python
 * reference, which reaches an endpoint through `urllib.parse.urlsplit` — so
 * whatever that refuses to parse, the reference refuses too, and this port has
 * to refuse identically or the two disagree about which console a tenant is
 * pointed at. The refusals live here rather than beside the grammars because
 * they are one self-contained question — "would CPython read this at all?" —
 * answered by a procedural port of three of its functions:
 *
 * * `_check_bracketed_netloc` / `_check_bracketed_host`, which decide whether
 *   brackets are placed and filled the way an authority requires.
 * * `ipaddress.IPv6Address`'s reading, which decides what may sit inside them.
 *
 * A bracketed *name* is not an address, and a bracketed IPv4 address is
 * refused outright, so only IPv6 (or the IPvFuture form RFC 3986 reserves)
 * gets through. Parity is pinned by the `graphicaltargets` golden corpus,
 * which records the real interpreter's answers.
 */

/** What CPython's `urlsplit` drops from a URL wherever it appears. */
const URL_BYTES_TO_REMOVE = /[\t\r\n]/g;

/**
 * The netloc of a `scheme://…`, as CPython's `urlsplit` finds it.
 *
 * Every caller has already put a scheme and its `//` in front, so what is
 * left is the authority up to whatever path, query or fragment ends it.
 */
export function netlocOf(url: string): string {
  // Dropped before anything is measured, as CPython drops them: a tab ahead
  // of the scheme would otherwise shift every index that follows.
  const cleaned = url.replace(URL_BYTES_TO_REMOVE, "");
  const after = cleaned.slice(cleaned.indexOf("://") + "://".length);
  const stop = after.search(/[/?#]/);
  return stop === -1 ? after : after.slice(0, stop);
}

/** How many hextets an address is written in, and the most it may take. */
const HEXTET_COUNT = 8;
const MAX_ADDRESS_CHARS = 45;

/** One hextet, one IPv4 octet, and the IPvFuture form RFC 3986 reserves. */
const HEXTET_PATTERN = /^[0-9A-Fa-f]{1,4}$/;
const OCTET_PATTERN = /^[0-9]{1,3}$/;
const IPV_FUTURE_PATTERN = /^v[0-9A-Fa-f]+\..+$/;

/**
 * Whether text is an IPv4 address, as strictly as CPython's `ipaddress` reads
 * one: four ASCII-decimal octets of at most three digits, none written with a
 * leading zero (glibc's `inet_pton` rule), and none above 255.
 */
function isIpv4Address(text: string): boolean {
  const octets = text.split(".");
  return (
    octets.length === 4 &&
    octets.every(
      (octet) => OCTET_PATTERN.test(octet) && (octet === "0" || !octet.startsWith("0")) && Number(octet) <= 255,
    )
  );
}

/**
 * Whether text is an IPv6 address, exactly as CPython's `ipaddress` reads one.
 *
 * A port of `IPv6Address`'s reading: an optional zone naming an interface, at
 * most 45 characters, eight hextets of at most four hex digits with at most one
 * `::` standing in for a run of zeroes, and a trailing IPv4 form counting as
 * two. A `/` is refused there too, but {@link netlocOf} stops at the first.
 */
function isIpv6Address(text: string): boolean {
  const zoneAt = text.indexOf("%");
  if (zoneAt !== -1) {
    // Everything past the first `%` is the zone, so a second one is not a zone
    // carrying a `%` — it is an address CPython refuses, as is an empty one.
    const zone = text.slice(zoneAt + 1);
    if (zone === "" || zone.includes("%")) {
      return false;
    }
  }
  const address = zoneAt === -1 ? text : text.slice(0, zoneAt);
  if (address === "" || address.length > MAX_ADDRESS_CHARS) {
    return false;
  }

  // Split as CPython splits, with the tail keeping any colons past the ninth
  // so an over-long address is counted rather than quietly truncated.
  const all = address.split(":");
  const parts =
    all.length > HEXTET_COUNT + 2 ? [...all.slice(0, HEXTET_COUNT + 1), all.slice(HEXTET_COUNT + 1).join(":")] : all;
  if (parts.length < 3) {
    return false;
  }
  if ((parts[parts.length - 1] as string).includes(".")) {
    if (!isIpv4Address(parts[parts.length - 1] as string)) {
      return false;
    }
    // An IPv4 tail is two hextets, so it is counted as two from here on.
    parts.splice(parts.length - 1, 1, "0", "0");
  }
  if (parts.length > HEXTET_COUNT + 1) {
    return false;
  }

  // A run of zeroes stood in for: one empty part, and never at an end, where
  // it is only the second half of the `::` that covers the endpoint.
  let skipAt = -1;
  for (let index = 1; index < parts.length - 1; index += 1) {
    if (parts[index] === "") {
      if (skipAt !== -1) {
        return false;
      }
      skipAt = index;
    }
  }
  let above: number;
  let below: number;
  if (skipAt === -1) {
    if (parts.length !== HEXTET_COUNT || parts[0] === "" || parts[parts.length - 1] === "") {
      return false;
    }
    above = HEXTET_COUNT;
    below = 0;
  } else {
    above = skipAt;
    below = parts.length - skipAt - 1;
    if (parts[0] === "") {
      above -= 1;
      if (above !== 0) {
        return false;
      }
    }
    if (parts[parts.length - 1] === "") {
      below -= 1;
      if (below !== 0) {
        return false;
      }
    }
    // A `::` that stands in for nothing is a `::` that was never needed.
    if (HEXTET_COUNT - (above + below) < 1) {
      return false;
    }
  }

  // Only the hextets either side of a skipped run are read, as CPython reads
  // them: the empty part between them is the run itself.
  return [...parts.slice(0, above), ...parts.slice(parts.length - below)].every((h) => HEXTET_PATTERN.test(h));
}

/**
 * Whether a bracketed host is one CPython's `_check_bracketed_host` accepts.
 *
 * An address or nothing: a name in brackets is not an address, and an IPv4 one
 * is refused outright — the same answer here, since only IPv6 gets through.
 */
function isBracketedHost(hostname: string): boolean {
  if (hostname.startsWith("v")) {
    return IPV_FUTURE_PATTERN.test(hostname);
  }
  return isIpv6Address(hostname);
}

/**
 * Whether CPython's `urlsplit` would read this netloc at all.
 *
 * Its two guards, in order: a bracket without its partner, and then
 * `_check_bracketed_netloc`, which insists nothing precedes the opening
 * bracket, that only a port follows the closing one, and that what is between
 * them is an address. Each raises where the URL is parsed, which the reference
 * turns into the refusal a hostless endpoint earns.
 */
export function bracketsAreValid(netloc: string): boolean {
  const opened = netloc.includes("[");
  if (opened !== netloc.includes("]")) {
    return false;
  }
  if (!opened) {
    return true;
  }
  // Split as `_hostinfo` splits, which is what the guard is written to mirror.
  const at = netloc.lastIndexOf("@");
  const hostinfo = at === -1 ? netloc : netloc.slice(at + 1);
  const open = hostinfo.indexOf("[");
  if (open === -1) {
    // Every bracket was in the credentials, so what is left is read as a
    // plain host — and is still held to being an address.
    const colon = hostinfo.indexOf(":");
    return isBracketedHost(colon === -1 ? hostinfo : hostinfo.slice(0, colon));
  }
  if (open !== 0) {
    return false;
  }
  const bracketed = hostinfo.slice(open + 1);
  const close = bracketed.indexOf("]");
  if (close === -1) {
    return isBracketedHost(bracketed);
  }
  const after = bracketed.slice(close + 1);
  return (after === "" || after.startsWith(":")) && isBracketedHost(bracketed.slice(0, close));
}
