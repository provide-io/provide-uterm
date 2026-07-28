//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The SSH server's security policy.
 *
 * Port of the policy in the Python module `provide.uterm.transports.ssh`.
 * Each rule here fails *open* when it is wrong:
 *
 * - With no validators the server accepts any credential. That is legitimate
 *   for a gateway that authenticates at the session layer — but only on a
 *   loopback bind, or with an explicit opt-in.
 * - A private key that is world-readable, or owned by somebody else, is not a
 *   secret. Loading it anyway would be silent.
 * - A per-IP connection count that never comes back down eventually locks a
 *   client, and everyone behind the same NAT, out of the server for good.
 */

import { ipAddress, isLoopback } from "../pycompat/ipaddress.ts";

/** Raised when a host key file is not safe to load. */
export class InsecureHostKeyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InsecureHostKeyError";
  }
}

/** Raised when a server would accept any credential where it must not. */
export class PermissiveAuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PermissiveAuthError";
  }
}

/** The only mode a host key may be found with. */
export const HOST_KEY_MODE = 0o600;

/** How many connections one address may hold at once. */
export const DEFAULT_MAX_CONNECTIONS_PER_IP = 5;

/** The bucket a connection with no peer address is counted under. */
const UNKNOWN_PEER = "unknown";

/** What a host key file's permissions look like. */
export interface KeyStat {
  /** The permission bits, without the file type. */
  mode: number;
  /** The owning user. */
  uid: number;
}

/** How a server was asked to authenticate. */
export interface ServerAuthOptions {
  /** The bind address. */
  host: string;
  /** Whether a password validator was supplied. */
  hasPasswordValidator?: boolean;
  /** Whether a public-key validator was supplied. */
  hasPublicKeyValidator?: boolean;
  /** Whether the operator explicitly opted into accepting any credential. */
  allowUnauthenticated?: boolean;
}

/** Render a mode the way CPython's `oct` does. */
function octal(mode: number): string {
  return `0o${mode.toString(8)}`;
}

/**
 * Whether `host` is a loopback-only bind address.
 *
 * The name is matched literally and nothing is resolved: this decides whether
 * an "accept any credential" server may start, so anything it is unsure about
 * has to count as public.
 */
export function isLoopbackBind(host: string): boolean {
  // Mirrors the reference's explicit empty check. Nothing observable rests on
  // it — an empty string does not parse as an address either — but "no host
  // given" is not a question about loopback, and saying so is worth a line.
  if (host === "") {
    return false;
  }
  if (host === "localhost") {
    return true;
  }
  const address = ipAddress(host);
  return address !== undefined && isLoopback(address);
}

/**
 * Check that a host key file is safe to load.
 *
 * @throws {InsecureHostKeyError} If the mode is anything but `0600`, or the
 *   file belongs to another user.
 */
export function verifyKeyPermissions(path: string, stat: KeyStat, currentUid?: number): void {
  // Exactly 0600, not "no wider than": a mode the server cannot write is one
  // it cannot rotate.
  if (stat.mode !== HOST_KEY_MODE) {
    throw new InsecureHostKeyError(
      `refusing to load SSH host key with insecure mode ${octal(stat.mode)} (expected ${octal(HOST_KEY_MODE)}): ${path}`,
    );
  }
  // Skipped where the platform has no user ids — refusing every key there
  // would make the server unstartable.
  if (currentUid !== undefined && stat.uid !== currentUid) {
    throw new InsecureHostKeyError(
      `refusing to load SSH host key owned by uid ${stat.uid} (current uid ${currentUid}): ${path}`,
    );
  }
}

/**
 * Check that the server will not accept any credential where it must not.
 *
 * @throws {PermissiveAuthError} When no validator is configured, the bind is
 *   not loopback, and nothing opted in.
 */
export function assertAuthenticationConfigured(options: ServerAuthOptions): void {
  if (
    options.hasPasswordValidator === true ||
    options.hasPublicKeyValidator === true ||
    options.allowUnauthenticated === true ||
    isLoopbackBind(options.host)
  ) {
    return;
  }
  throw new PermissiveAuthError(
    "refusing to start an SSH server that accepts any credential on a non-loopback " +
      `bind ('${options.host}'); pass credentials_validator / public_key_validator, bind to ` +
      "loopback, or set allow_unauthenticated=True to opt in explicitly",
  );
}

/** A connection's claim on a slot, given back when it ends. */
export interface ConnectionSlot {
  /** Give the slot back. Safe to call twice. */
  release(): void;
}

/**
 * Counts concurrent connections per address, and caps them.
 *
 * The slot is handed to the connection rather than tracked here, mirroring
 * the reference, where each connection holds its own peer and they share one
 * table. A limiter that remembered only the last admission would credit a
 * *rejected* connection's close to the one before it, and let the next one
 * through.
 */
export class ConnectionLimiter {
  readonly #maxPerIp: number;
  readonly #counts = new Map<string, number>();

  constructor(maxPerIp: number = DEFAULT_MAX_CONNECTIONS_PER_IP) {
    this.#maxPerIp = maxPerIp;
  }

  /**
   * Claim a slot for a connection from `peer`, or refuse it.
   *
   * A connection with no peer address is counted too, under its own bucket:
   * being unidentifiable is not a reason to be exempt.
   */
  admit(peer: readonly [string, number] | undefined): ConnectionSlot | undefined {
    const key = peer === undefined ? UNKNOWN_PEER : peer[0];
    const count = this.#counts.get(key) ?? 0;
    if (count >= this.#maxPerIp) {
      return undefined;
    }
    this.#counts.set(key, count + 1);
    let released = false;
    return {
      release: () => {
        if (released) {
          return;
        }
        released = true;
        this.#release(key);
      },
    };
  }

  /** The live counts, for a caller that reports them. */
  counts(): Record<string, number> {
    return Object.fromEntries(this.#counts);
  }

  /**
   * Give one connection's slot back.
   *
   * Only a slot handed out by `admit` reaches here, and only once, so the
   * bucket is always present — hence the assertion rather than a fallback
   * that could never be taken.
   */
  #release(key: string): void {
    const count = (this.#counts.get(key) as number) - 1;
    if (count <= 0) {
      // Dropped rather than left at zero, or the table grows without bound
      // for the life of the process.
      this.#counts.delete(key);
      return;
    }
    this.#counts.set(key, count);
  }
}

/**
 * Whether a password is accepted.
 *
 * With no validator anything is — legitimate for a gateway that authenticates
 * at the session layer, and exactly why {@link assertAuthenticationConfigured}
 * exists.
 */
export function validatePassword(
  user: string,
  password: string,
  validator?: (user: string, password: string) => boolean,
): boolean {
  return validator === undefined ? true : validator(user, password);
}

/** Whether a public key is accepted. Permissive by default, as above. */
export function validatePublicKey(
  user: string,
  key: unknown,
  validator?: (user: string, key: unknown) => boolean,
): boolean {
  return validator === undefined ? true : validator(user, key);
}
