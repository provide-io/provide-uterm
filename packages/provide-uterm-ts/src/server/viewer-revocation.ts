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
 * How many per-session cutoffs are held at once. Past it the oldest is
 * forgotten, which can only matter for a share token that outlived thousands
 * of later revocations.
 */
export const SHARE_VIEWER_REVOCATIONS_MAX = 10000;

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

  /**
   * Refuse every share-viewer token for a session issued at or before `now`
   * (wall-clock seconds, the clock a token's `iat` is written in) and return
   * the cutoff. A token issued in the same second is refused too: `iat` has
   * one-second resolution, so it cannot say which side it fell on.
   */
  revoke(sessionId: string, now: number): number {
    this.#cutoffs.delete(sessionId);
    this.#cutoffs.set(sessionId, now);
    for (const oldest of this.#cutoffs.keys()) {
      if (this.#cutoffs.size <= SHARE_VIEWER_REVOCATIONS_MAX) {
        break;
      }
      this.#cutoffs.delete(oldest);
    }
    return now;
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
