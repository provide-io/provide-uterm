//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Which session a request is for, and who may redeem a tunnel invite.
 *
 * Port of the admission half of
 * `provide.uterm.cloudflare.do.session_runtime.fetch`.
 *
 * The Durable Object's own identity comes back as `default` on the Cloudflare
 * Python runtime, so the session's id is recovered from the URL path instead.
 * A segment is taken only when it is non-empty, and — on the invite route,
 * which is the one that decodes — only when it carries no slash of its own. A
 * segment holding an encoded slash would otherwise name a different session
 * than the path appears to address.
 *
 * The invite-redemption route is emitted only by the Worker when it proxies
 * `/s/{id}`, and is deliberately absent from the public route table. Three
 * things must hold or the answer is 404: the internal provenance header
 * matches, the session id is well-formed, and it is *this* session. Any one of
 * them missing means somebody found the route rather than being sent to it.
 */

/** The route the Worker proxies an invite redemption to. */
export const INVITE_REDEEM_PREFIX = "/_internal/tunnel-invite/";

/** The header that says the Worker sent this, rather than a browser. */
export const INVITE_REDEEM_HEADER = "X-Provide-Uterm-Internal";

/** What that header has to say. */
export const INVITE_REDEEM_PROVENANCE = "worker-invite-redemption-v1";

/** The identity a Durable Object reports when it does not know its own. */
export const UNKNOWN_WORKER_ID = "default";

/**
 * The route prefixes a session id follows.
 *
 * Ordered as the reference orders them; the first match wins, and none of them
 * is a prefix of another so the order does not change any answer.
 */
export const WORKER_ID_PREFIXES: readonly string[] = [
  "/ws/worker/",
  "/ws/browser/",
  "/ws/raw/",
  "/tunnel/",
  "/worker/",
  "/api/sessions/",
];

/**
 * The session id a URL names, or nothing when it names none.
 *
 * Called only when the runtime does not already know its own id — a runtime
 * that does keeps it, because the URL is the caller's and the identity is not.
 *
 * @param currentWorkerId What the runtime already believes.
 * @param url The request's URL, which may be unreadable.
 */
export function lazyWorkerId(currentWorkerId: string, url: string | undefined): string {
  if (currentWorkerId !== UNKNOWN_WORKER_ID) {
    return currentWorkerId;
  }
  const path = pathOf(url);
  if (path === undefined) {
    // An unreadable URL leaves the identity alone rather than clearing it:
    // there is nothing better to put there.
    return currentWorkerId;
  }

  if (path.startsWith(INVITE_REDEEM_PREFIX)) {
    const parts = path.split("/");
    // `["", "_internal", "tunnel-invite", "<id>", "redeem"]` and nothing else.
    if (parts.length === 5 && parts[4] === "redeem") {
      // Decoded here and nowhere else, which is why the slash check below
      // belongs here too: `%2F` becomes a separator only on this route.
      const sessionId = decodeSegment(parts[3] as string);
      if (sessionId !== "" && !sessionId.includes("/")) {
        return sessionId;
      }
    }
    // Returned rather than falling through, which is unobservable today —
    // no route prefix begins with the invite prefix, and a test pins that —
    // but keeps the two route families from ever answering for each other.
    return currentWorkerId;
  }

  for (const prefix of WORKER_ID_PREFIXES) {
    if (path.startsWith(prefix)) {
      // Not decoded: an encoded slash stays encoded and cannot become a
      // separator, so this segment is whatever the path literally said.
      const segment = path.slice(prefix.length).split("/")[0] as string;
      if (segment !== "") {
        return segment;
      }
      return currentWorkerId;
    }
  }
  return currentWorkerId;
}

/**
 * Whether an invite redemption may proceed.
 *
 * All three conditions, and the reason each is here: the provenance header
 * says the Worker sent this rather than a browser finding the route; a
 * well-formed id says the path was not crafted; and matching *this* session
 * says the request reached the object it names rather than any other.
 */
export function inviteRedemptionAllowed(provenance: string | undefined, sessionId: string, workerId: string): boolean {
  return (
    provenance === INVITE_REDEEM_PROVENANCE && sessionId !== "" && !sessionId.includes("/") && sessionId === workerId
  );
}

/**
 * The session id an invite-redemption path names, or an empty string.
 *
 * Empty means the path was not one, which the caller turns into a 404 for the
 * same reason a bad provenance does.
 */
export function inviteSessionId(path: string): string {
  if (!path.startsWith(INVITE_REDEEM_PREFIX)) {
    return "";
  }
  const parts = path.split("/");
  if (parts.length !== 5 || parts[4] !== "redeem") {
    return "";
  }
  return decodeSegment(parts[3] as string);
}

/** The path of a URL, or nothing when it cannot be read. */
function pathOf(url: string | undefined): string | undefined {
  if (url === undefined) {
    return undefined;
  }
  try {
    return new URL(url).pathname;
  } catch {
    return undefined;
  }
}

/** One path segment, decoded as `urllib.parse.unquote` decodes it. */
function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    // A malformed escape is left as it was, which is what `unquote` does.
    return segment;
  }
}
