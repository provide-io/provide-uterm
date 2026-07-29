//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What an SSH server will start with, and what it will accept once it has.
 *
 * Port of the admission rules in `provide.uterm.transports.ssh`.
 *
 * An SSH server that accepts any password is a reasonable thing on a loopback
 * bind — a gateway authenticating at the session layer above it — and a very
 * bad one on a routable address. That difference is the whole of this module:
 * with neither validator set, every password and every key is accepted, so the
 * only thing between the world and a shell would be the bind address. Starting
 * that way on anything but loopback is refused unless an operator writes the
 * opt-in down.
 *
 * A host key must be private to its owner, because a key another account can
 * read is a key another account can impersonate the server with. And
 * connections are counted per address, so one host cannot take the server's
 * whole capacity by itself.
 */

import { ipAddress, isLoopback } from "../pycompat/index.ts";

/** How many connections one address may hold at once, when nobody says. */
export const DEFAULT_MAX_CONNECTIONS_PER_IP = 5;

/** The mode a host key file must have. */
export const REQUIRED_HOST_KEY_MODE = 0o600;

/** A host key that anybody else could read, or that belongs to somebody else. */
export class HostKeyPermissionError extends Error {}

/**
 * Whether a bind address reaches only this machine.
 *
 * Matched by name for `localhost` exactly and otherwise by parsing the
 * address. The name check is case-sensitive, as the reference's is: `LOCALHOST`
 * is *not* recognised, so a server bound to it is treated as routable and has
 * to authenticate. That is the safe direction to be wrong in, and the reason
 * it is left alone.
 */
export function isLoopbackBind(host: string): boolean {
  // No separate check for an empty host: it is not the name, and it does not
  // parse as an address, so it falls out below as not loopback.
  if (host === "localhost") {
    return true;
  }
  const address = ipAddress(host);
  if (address === undefined) {
    // A name nobody can resolve here is not loopback as far as this is
    // concerned — resolving it would be a lookup in a start-up check, and a
    // name that resolves to loopback today may not tomorrow.
    return false;
  }
  return isLoopback(address);
}

/** What a caller is starting a server with. */
export interface SshServerAdmission {
  /** Whether a password validator was supplied. */
  hasPasswordValidator: boolean;
  /** Whether a public-key validator was supplied. */
  hasPublicKeyValidator: boolean;
  /**
   * Whether the caller explicitly accepted an unauthenticated server.
   *
   * The word an operator has to write down. Without it, the combination below
   * does not start.
   */
  allowUnauthenticated: boolean;
  host: string;
}

/**
 * Whether a server may start with these settings.
 *
 * Refused only for the one combination that leaves nothing checking anything:
 * no validator of either kind, no opt-in, and a bind that is not loopback.
 * Either validator alone is enough to proceed — a server that checks passwords
 * but accepts any key is the caller's decision to make, and not one this can
 * second-guess.
 */
export function sshServerMayStart(admission: SshServerAdmission): boolean {
  const nothingChecks = !admission.hasPasswordValidator && !admission.hasPublicKeyValidator;
  return !(nothingChecks && !admission.allowUnauthenticated && !isLoopbackBind(admission.host));
}

/**
 * Check that a host key file is private.
 *
 * The mode is matched *exactly*, not as an upper bound — so `0400` is refused
 * along with `0644`. That is the reference's behaviour: a key the server
 * cannot write is as much a surprise as one everybody can read, and either
 * means the file is not the one this server manages.
 *
 * @param mode The file's permission bits, already masked.
 * @param ownerUid Who owns the file.
 * @param currentUid Who is running, or nothing on a platform without uids.
 * @throws {HostKeyPermissionError} On a mode that is not exactly 0600, or a
 *   file owned by somebody else.
 */
export function verifyHostKeyPermissions(
  path: string,
  mode: number,
  ownerUid: number,
  currentUid: number | undefined,
): void {
  if (mode !== REQUIRED_HOST_KEY_MODE) {
    throw new HostKeyPermissionError(
      `refusing to load SSH host key with insecure mode ${pyOct(mode)} (expected 0o600): ${path}`,
    );
  }
  if (currentUid !== undefined && ownerUid !== currentUid) {
    throw new HostKeyPermissionError(
      `refusing to load SSH host key owned by uid ${ownerUid} (current uid ${currentUid}): ${path}`,
    );
  }
}

/**
 * Counts of how many connections each address holds.
 *
 * Kept as a plain map so the caller owns the lifetime: a server that restarted
 * has no connections, and this should not remember otherwise.
 */
export type IpConnectionCounts = Map<string, number>;

/**
 * Whether one more connection from this address may be accepted.
 *
 * At the limit, not past it: the count is what is already held, so a limit of
 * one means the second connection is refused.
 */
export function connectionAllowed(counts: IpConnectionCounts, ip: string, limit: number): boolean {
  return (counts.get(ip) ?? 0) < limit;
}

/** Record one accepted connection. */
export function noteConnectionOpened(counts: IpConnectionCounts, ip: string): void {
  counts.set(ip, (counts.get(ip) ?? 0) + 1);
}

/**
 * Record one connection going.
 *
 * The entry is removed once it reaches zero rather than left at nothing, so a
 * server that has seen many addresses does not accumulate one entry per
 * address it has ever met.
 */
export function noteConnectionClosed(counts: IpConnectionCounts, ip: string): void {
  const remaining = (counts.get(ip) ?? 0) - 1;
  if (remaining <= 0) {
    counts.delete(ip);
    return;
  }
  counts.set(ip, remaining);
}

/** A permission mode as Python's `oct()` writes it. */
function pyOct(mode: number): string {
  return `0o${mode.toString(8)}`;
}
