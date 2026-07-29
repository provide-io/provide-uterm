//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * One-time tunnel invite bootstrap.
 *
 * Port of `provide.uterm.server.tunnel_invites`. An invite is what somebody
 * redeems to get the share or control token for a session they were handed a
 * link to — the whole bootstrap, so what it refuses is the point:
 *
 * * **The store holds hashes.** A memory disclosure leaks digests, which
 *   redeem nothing.
 * * **One use.** The entry is removed before anything about it is checked, so
 *   an attempt refused for being expired or for naming the wrong session has
 *   still spent the invite.
 * * **Five minutes, or the tunnel's own life, whichever ends first.** An
 *   invite cannot outlive the tunnel it opens.
 * * **The session is named by the caller and checked**, so a link for one
 *   session cannot be redeemed against another.
 */

import { randomBytes } from "node:crypto";
import { hashToken, verifyToken } from "./token-hash.ts";

/** The roles an invite can carry. Neither is `admin`. */
export type TunnelInviteRole = "viewer" | "operator";

/** How long an invite lives, unless the tunnel ends sooner. */
export const INVITE_TTL_S = 300;

/** The bytes behind an invite, matching the reference's `token_urlsafe(32)`. */
const INVITE_BYTES = 32;

/** A redeemed invite. */
export interface TunnelInvite {
  sessionId: string;
  role: TunnelInviteRole;
  tunnelToken: string;
  expiresAt: number;
  issuedIp?: string | undefined;
}

/** Invites at rest, keyed by the invite's hash — never by the invite. */
export type InviteStore = Map<string, Record<string, unknown>>;

/** What issuing a pair of invites needs to know. */
export interface IssueOptions {
  sessionId: string;
  shareToken: string;
  controlToken: string;
  tunnelExpiresAt: number;
  issuedIp: string | null;
  now: number;
}

/** A URL-safe token of the same length and alphabet as the reference's. */
function inviteToken(): string {
  return randomBytes(INVITE_BYTES).toString("base64url");
}

/**
 * Mint a viewer and an operator invite, storing only their hashes.
 *
 * @returns The two invites, viewer first — the only place they exist in the
 * clear.
 */
export function issueTunnelInvites(store: InviteStore, options: IssueOptions): [string, string] {
  const inviteExpiresAt = Math.min(options.tunnelExpiresAt, options.now + INVITE_TTL_S);
  const shareInvite = inviteToken();
  const controlInvite = inviteToken();
  store.set(hashToken(shareInvite), {
    session_id: options.sessionId,
    role: "viewer",
    tunnel_token: options.shareToken,
    expires_at: inviteExpiresAt,
    issued_ip: options.issuedIp,
  });
  store.set(hashToken(controlInvite), {
    session_id: options.sessionId,
    role: "operator",
    tunnel_token: options.controlToken,
    expires_at: inviteExpiresAt,
    issued_ip: options.issuedIp,
  });
  return [shareInvite, controlInvite];
}

/**
 * Whether a stored expiry is a number that can be compared against.
 *
 * A boolean is refused here where CPython refuses it by `float(True)` being
 * `1.0` — an instant long past, so both refuse the invite.
 *
 * `NaN` and an infinity are refused where the reference takes them: `now >
 * nan` is false in both languages, so an invite carrying either never expires
 * there. Neither can arrive through JSON, and the refusal is the safe
 * direction, so this is a deliberate divergence rather than a copied hole.
 */
function readExpiry(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/**
 * Redeem an invite, or refuse it.
 *
 * The entry is removed first, so the invite is spent either way.
 *
 * @returns The invite, or nothing when it does not exist, has expired, names
 * another session, carries a role this system does not have, or carries no
 * token to hand over.
 */
export function consumeTunnelInvite(
  store: InviteStore,
  invite: string,
  sessionId: string,
  now: number,
): TunnelInvite | undefined {
  const offered = invite.trim();
  if (offered === "") {
    // Refused before the store is touched, so a caller sending nothing cannot
    // evict somebody else's invite.
    return undefined;
  }
  const inviteHash = hashToken(offered);
  const raw = store.get(inviteHash);
  // Before the entry is looked at, so a refused attempt has still spent it.
  // Removing it only on success would answer the same for every store this
  // code writes — the difference shows on a store somebody else wrote.
  store.delete(inviteHash);
  if (raw === undefined) {
    return undefined;
  }
  const expiresAt = readExpiry(raw.expires_at);
  if (expiresAt === undefined || now > expiresAt) {
    return undefined;
  }
  if (String(raw.session_id ?? "") !== sessionId) {
    return undefined;
  }
  const role = String(raw.role ?? "");
  if (role !== "viewer" && role !== "operator") {
    return undefined;
  }
  const tunnelToken = String(raw.tunnel_token ?? "").trim();
  if (tunnelToken === "") {
    return undefined;
  }
  const issuedIp = raw.issued_ip;
  return {
    // The caller's, which the check above proved is also the invite's.
    sessionId,
    role,
    tunnelToken,
    expiresAt,
    issuedIp: issuedIp === null || issuedIp === undefined ? undefined : String(issuedIp),
  };
}

/** Drop every pending invite for one session. */
export function discardTunnelInvitesForSession(store: InviteStore, sessionId: string): void {
  for (const [key, value] of [...store]) {
    if (String(value.session_id ?? "") === sessionId) {
      store.delete(key);
    }
  }
}

/**
 * Drop every invite past its expiry.
 *
 * An entry whose expiry cannot be read is left in place: redeeming refuses it
 * anyway, so sweeping it would only hide a store somebody wrote wrong.
 */
export function sweepExpiredTunnelInvites(store: InviteStore, now: number): void {
  for (const [key, value] of [...store]) {
    const expiresAt = readExpiry(value.expires_at);
    if (expiresAt !== undefined && now > expiresAt) {
      store.delete(key);
    }
  }
}

/**
 * Whether a redeemed invite still carries the tunnel's current token.
 *
 * An invite minted before a rotation carries the old one; this is what
 * catches it.
 */
export function tunnelInviteMatchesTokenHash(invite: TunnelInvite, tokenHash: string): boolean {
  return verifyToken(invite.tunnelToken, tokenHash);
}
