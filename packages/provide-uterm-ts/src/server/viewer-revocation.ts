//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Revoking a session's share viewers.
 *
 * Port of `revoke_share_viewers` / `share_viewer_revoked` on the reference's
 * `AuthorizationService`. A share viewer is a `share:{session_id}:...` subject
 * holding nothing beyond the viewer role and no session-scoped admin grant —
 * the token an embedder mints so someone can watch one session. Its token used
 * to be judged only when it was presented, so nothing could take a watch away
 * short of waiting for the token to expire. A per-session cutoff refuses every
 * such token issued at or before it.
 *
 * Process-local, like the tunnel token store.
 */

import type { AuthorizablePrincipal } from "./authorization.ts";

/**
 * The close code a revoked share viewer's socket gets: an application code
 * (4000-4999) so a client can tell "access was taken away" from a drop or a
 * policy refusal and stop reconnecting. 4410 echoes HTTP 410 Gone. This server
 * binds no browser socket, so it never sends it; it reports it so a client
 * written against any port reads the same answer.
 */
export const VIEWER_REVOKED_CLOSE_CODE = 4410;

/** Deliberately generic: the embedder knows why, and says so in its own words. */
export const VIEWER_REVOKED_REASON = "viewer access revoked";

/**
 * How many per-session cutoffs are held at once. When the table is full a
 * revoke for a session not already in it is refused (the route answers 429)
 * rather than evicting a cutoff that may still protect another session.
 */
export const SHARE_VIEWER_REVOCATIONS_MAX = 10000;

/**
 * How many cutoffs one revoking principal holds at once, so no single owner
 * can fill the table. Past it, that principal's own oldest cutoff makes room
 * for its newest. A global admin is not capped.
 */
export const SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX = 100;

/**
 * How long a cutoff is kept, in seconds. Share tokens must live well under
 * this: a token that outlives its cutoff is admitted again.
 */
export const SHARE_VIEWER_REVOCATION_TTL_S = 86400;

/** The cutoff table is full of live cutoffs; a new session cannot be added. */
export class ShareViewerRevocationsFullError extends Error {
  constructor() {
    super("share-viewer revocation table is full");
    this.name = "ShareViewerRevocationsFullError";
  }
}

/** A principal as a revocation reads it: who, what roles, and its token's claims. */
export interface RevocablePrincipal extends AuthorizablePrincipal {
  claims?: Readonly<Record<string, unknown>> | undefined;
}

/** Whether a principal is a read-only share grant for one session. */
export function isShareViewer(principal: AuthorizablePrincipal, sessionId: string): boolean {
  return (
    (principal.admin_session_scope ?? null) === null &&
    principal.subject_id.startsWith(`share:${sessionId}:`) &&
    [...principal.roles].every((role) => role === "viewer")
  );
}

/** The per-session cutoffs. */
export class ShareViewerRevocations {
  // A Map iterates in insertion order, so the first key is the oldest cutoff.
  readonly #cutoffs = new Map<string, number>();
  // A session's capped revoker, and each such principal's sessions, oldest first.
  readonly #revokers = new Map<string, string>();
  readonly #revokedBy = new Map<string, Set<string>>();

  /** How many cutoffs are held. */
  get size(): number {
    return this.#cutoffs.size;
  }

  /**
   * Refuse every share-viewer token for a session issued at or before `now`
   * (wall-clock seconds, the clock a token's `iat` is written in) and return
   * the cutoff. A token issued in the same second is refused too: `iat` has
   * one-second resolution, so it cannot say which side it fell on. Throws
   * {@link ShareViewerRevocationsFullError} when the table is full and this
   * session holds no cutoff yet. The cutoff counts against `revoker`, who
   * holds at most {@link SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX} of them,
   * its oldest evicted first; no revoker (a global admin) is not capped.
   */
  revoke(sessionId: string, now: number, revoker?: string): number {
    // Insertion order is revocation order, so the stale cutoffs are in front.
    for (const [oldest, cutoff] of this.#cutoffs) {
      if (cutoff >= now - SHARE_VIEWER_REVOCATION_TTL_S) {
        break;
      }
      this.#drop(oldest);
    }
    const seen = this.#cutoffs.has(sessionId);
    this.#drop(sessionId);
    const mine = revoker === undefined ? undefined : this.#revokedBy.get(revoker);
    if (mine !== undefined && mine.size >= SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX) {
      this.#drop(mine.values().next().value as string);
    }
    // Nothing was dropped above unless this session or the revoker's own
    // oldest made room, so a refusal leaves the table as it was.
    if (!seen && this.#cutoffs.size >= SHARE_VIEWER_REVOCATIONS_MAX) {
      throw new ShareViewerRevocationsFullError();
    }
    this.#cutoffs.set(sessionId, now);
    if (revoker !== undefined) {
      this.#revokers.set(sessionId, revoker);
      const sessions = this.#revokedBy.get(revoker) ?? new Set<string>();
      sessions.add(sessionId);
      this.#revokedBy.set(revoker, sessions);
    }
    return now;
  }

  #drop(sessionId: string): void {
    this.#cutoffs.delete(sessionId);
    const revoker = this.#revokers.get(sessionId);
    if (revoker === undefined) {
      return;
    }
    this.#revokers.delete(sessionId);
    const mine = this.#revokedBy.get(revoker) as Set<string>;
    mine.delete(sessionId);
    if (mine.size === 0) {
      this.#revokedBy.delete(revoker);
    }
  }

  /** The cutoff recorded for a session, if its viewers were revoked. */
  cutoff(sessionId: string): number | undefined {
    return this.#cutoffs.get(sessionId);
  }

  /**
   * Whether a principal is a share viewer of the session whose token predates
   * the cutoff. A token without a numeric `iat` cannot show it postdates the
   * cutoff, so it is refused.
   */
  revoked(principal: RevocablePrincipal, sessionId: string): boolean {
    const cutoff = this.#cutoffs.get(sessionId);
    if (cutoff === undefined || !isShareViewer(principal, sessionId)) {
      return false;
    }
    const issued = principal.claims?.iat;
    return typeof issued !== "number" || Number.isNaN(issued) || issued <= cutoff;
  }
}
