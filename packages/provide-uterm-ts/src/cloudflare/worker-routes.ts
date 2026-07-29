//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The route matchers a request meets before anything asks who it is.
 *
 * Port of the matching in `provide.uterm.cloudflare.entry.handlers`,
 * `.entry.spa` and `.entry.registry`.
 *
 * **The public checks are a closed set, not a blocklist.** A static file is
 * served only when its name is `.html`, `.css` or `.js` *and* is made of
 * letters, digits, dots, slashes, dashes and underscores. Anything else is not
 * refused here — it falls through to the routes that do ask who is calling,
 * which is the safe direction. Widening any of these is how a Worker starts
 * handing out pages to nobody in particular.
 *
 * **A Durable Object route bounds its session id.** Sixty-four characters of a
 * closed alphabet, so a proxied path cannot name an object by a string that a
 * filesystem or a KV key would read differently than this does.
 */

/** The path that answers without asking anything. */
export const HEALTH_PATH = "/api/health";

/** Everything under here is served as an asset. */
export const ASSET_PREFIX = "/assets/";

/**
 * A static file this Worker will serve.
 *
 * The alphabet excludes `%`, so an encoded separator never reaches the asset
 * loader; and the extension set is closed, so a file the build did not mean to
 * publish — a `.json` of configuration, say — is not served by this route at
 * all.
 */
export const STATIC_ASSET_PATTERN = /^\/[a-zA-Z0-9._/-]+\.(?:html|css|js)$/;

/** What a public check found, if anything. */
export interface PublicRoute {
  kind: "health" | "asset" | "static";
  /** The name to load, relative to whichever root serves it. */
  name?: string;
}

/**
 * Whether this path is served without asking who is calling.
 *
 * @returns What to serve, or nothing when the path is not public — in which
 *   case the caller goes on to the routes that do ask.
 */
export function publicRoute(path: string): PublicRoute | undefined {
  if (path === HEALTH_PATH) {
    return { kind: "health" };
  }
  if (path.startsWith(ASSET_PREFIX)) {
    return { kind: "asset", name: path.slice(ASSET_PREFIX.length) };
  }
  if (STATIC_ASSET_PATTERN.test(path)) {
    return { kind: "static", name: path.slice(1) };
  }
  return undefined;
}

/** A page the single-page app serves. */
export type SpaPageKind = "dashboard" | "connect" | "share" | "session" | "inspect" | "replay" | "operator";

/** Which page a path names, and what it is bootstrapped with. */
export interface SpaRoute {
  kind: SpaPageKind;
  sessionId?: string;
  /** Which of the two surfaces the page is built for. */
  surface?: "user" | "operator";
}

/** `/s/{id}` — a share link, which is how somebody without an account arrives. */
const SHARE_ROUTE = /^\/s\/(?<sid>[a-zA-Z0-9_-]{1,64})$/;

/** `/app/{kind}/{id}` for the four session-scoped pages. */
const SPA_SESSION_ROUTE = /^\/app\/(?<kind>session|inspect|replay|operator)\/(?<sid>[a-zA-Z0-9_-]{1,64})$/;

/**
 * Which single-page route a path names, or nothing.
 *
 * The surface follows the page kind: an inspect, replay or operator page is an
 * operator surface and a session page is a user one, so a page cannot be
 * reached with more of the interface than its kind implies.
 */
export function resolveSpaRoute(path: string): SpaRoute | undefined {
  if (path === "/" || path === "/app" || path === "/app/") {
    return { kind: "dashboard" };
  }
  if (path === "/app/connect" || path === "/app/connect/") {
    return { kind: "connect" };
  }
  const share = SHARE_ROUTE.exec(path);
  if (share !== null) {
    return { kind: "share", sessionId: share.groups?.sid as string, surface: "user" };
  }
  const session = SPA_SESSION_ROUTE.exec(path);
  if (session !== null) {
    const kind = session.groups?.kind as SpaPageKind;
    return {
      kind,
      sessionId: session.groups?.sid as string,
      surface: kind === "session" ? "user" : "operator",
    };
  }
  return undefined;
}

/**
 * The routes proxied straight to a session's Durable Object.
 *
 * Each bounds the session id to a closed alphabet and sixty-four characters,
 * because this string becomes an object name.
 */
export const DO_ROUTE_PATTERNS: readonly RegExp[] = [
  /^\/ws\/browser\/(?<worker_id>[a-zA-Z0-9_-]{1,64})\/term$/,
  /^\/ws\/worker\/(?<worker_id>[a-zA-Z0-9_-]{1,64})\/term$/,
  /^\/ws\/raw\/(?<worker_id>[a-zA-Z0-9_-]{1,64})\/term$/,
  /^\/tunnel\/(?<worker_id>[a-zA-Z0-9_-]{1,64})$/,
  /^\/worker\/(?<worker_id>[a-zA-Z0-9_-]{1,64})\/hijack(?:\/.*)?$/,
  /^\/worker\/(?<worker_id>[a-zA-Z0-9_-]{1,64})\/(?:input_mode|disconnect_worker)$/,
];

/**
 * The session a Durable Object route names, or nothing.
 *
 * Nothing means the path is not one of these, which the caller turns into a
 * 404 rather than guessing an object name from it.
 */
export function extractWorkerId(path: string): string | undefined {
  for (const pattern of DO_ROUTE_PATTERNS) {
    const match = pattern.exec(path);
    if (match !== null) {
      return match.groups?.worker_id as string;
    }
  }
  return undefined;
}
