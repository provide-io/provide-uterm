//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What the hijack client checks before it talks to a server or writes a log.
 *
 * Port of the guards in `provide.uterm.client.hijack`.
 *
 * * **An identifier is one path segment or it is refused.** A worker id
 *   holding a slash, a dot-dot or a query string would forge a route — asking
 *   a server for something the caller never named. The check is a whitelist
 *   rather than an escape, because escaping is where this kind of bug lives.
 * * **Nothing sensitive is written down.** A failed request is logged with its
 *   body, and a body can hold a token. Anything whose key looks like a secret
 *   is replaced outright, and long strings and long lists are cut — a log that
 *   costs a megabyte per failure is a log nobody keeps.
 */

/** What an identifier may be made of: one path segment, and nothing clever. */
const ID_PATTERN = /^[A-Za-z0-9._-]+$/;

/** Identifiers that are a path all by themselves. */
const PATH_IDS = new Set([".", ".."]);

/** Key fragments that mean the value must not be written down. */
const SENSITIVE = ["token", "secret", "password", "key", "auth", "session_id"] as const;

/** How much of a string is kept. */
export const MAX_STRING = 500;

/** How many items of a list are kept. */
export const MAX_ITEMS = 10;

/** What a redacted value is replaced with. */
export const REDACTED = "***";

/**
 * Check an identifier is one safe path segment.
 *
 * @throws {Error} For anything that is not — including a single dot or two,
 *   which are a path rather than a name.
 * @returns The identifier, so this reads as a step in building a path.
 */
export function safeId(value: string, kind = "id"): string {
  // The empty check and the pattern's `+` each refuse an empty identifier on
  // their own, so no test can tell one from the other. Both are kept: the
  // check says the rule, and the `+` enforces it even if the pattern is ever
  // rewritten.
  if (value === "" || PATH_IDS.has(value) || !ID_PATTERN.test(value)) {
    throw new Error(`invalid ${kind}: ${quote(value)}`);
  }
  return value;
}

/** Whether a key names something that must not be logged. */
function isSensitive(key: string): boolean {
  const lowered = key.toLowerCase();
  // Contained, not equal: `api_key` and `x-auth-token` are both secrets, and a
  // list of exact names would miss the next one somebody adds.
  return SENSITIVE.some((fragment) => lowered.includes(fragment));
}

/**
 * Strip anything sensitive and cut anything long.
 *
 * A sensitive value is replaced rather than shortened — half a token is still
 * half a token.
 */
export function sanitize(value: unknown): unknown {
  if (Array.isArray(value)) {
    if (value.length > MAX_ITEMS) {
      // The marker says the list was cut, so a reader does not take the first
      // ten for all of them.
      return [...value.slice(0, MAX_ITEMS).map(sanitize), "..."];
    }
    return value.map(sanitize);
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, isSensitive(key) ? REDACTED : sanitize(item)]),
    );
  }
  if (typeof value === "string" && value.length > MAX_STRING) {
    return `${value.slice(0, MAX_STRING)}...`;
  }
  return value;
}

/** Where a worker's own routes live. */
export function workerPath(entityPrefix: string, workerId: string): string {
  return `${trimSlashes(entityPrefix)}/${safeId(workerId, "worker_id")}`;
}

/** Where one hijack of one worker lives. */
export function hijackPath(entityPrefix: string, workerId: string, hijackId: string): string {
  return `${workerPath(entityPrefix, workerId)}/hijack/${safeId(hijackId, "hijack_id")}`;
}

/** Where a session lives. */
export function sessionPath(sessionId: string): string {
  return `/api/sessions/${safeId(sessionId, "session_id")}`;
}

/** Drop trailing slashes, so a prefix given either way builds one path. */
function trimSlashes(prefix: string): string {
  return prefix.replace(/\/+$/, "");
}

/**
 * A value as Python's `repr` quotes it, which is how the messages read.
 *
 * Printable characters outside ASCII are left as themselves — `repr`
 * escapes only what cannot be shown, so a name in another script reads as
 * that name rather than as a row of code points.
 */
function quote(value: string): string {
  const escaped = value
    .replaceAll("\\", "\\\\")
    .replaceAll("\n", "\\n")
    .replaceAll("\r", "\\r")
    .replaceAll("\t", "\\t")
    // Control bytes are named literally here: rendering them visibly is the point.
    .replaceAll(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, (character) => {
      return `\\x${character.charCodeAt(0).toString(16).padStart(2, "0")}`;
    });
  return escaped.includes("'") && !escaped.includes('"') ? `"${escaped}"` : `'${escaped.replaceAll("'", "\\'")}'`;
}
