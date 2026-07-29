//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a PAM notification says, and what is refused before it is believed.
 *
 * Port of the parsing and admission rules in
 * `provide.uterm.pty.pam_listener`. An event arrives as one JSON line on a
 * Unix socket and can start a session on somebody's behalf, so what this
 * refuses is the boundary.
 *
 * Anything it does not understand becomes nothing: a line that is not JSON, an
 * event outside the two it knows, or an event with no username is dropped and
 * the connection stays up. One bad line from a confused sender must not end a
 * listener that other logins depend on. The mode is narrowed rather than
 * trusted — anything that is not exactly `capture` is `notify` — so a sender
 * cannot reach the capture path by spelling it differently. And a pid that is
 * not a number becomes zero rather than failing the event, because the pid is
 * advisory where the username is not.
 *
 * What this does *not* do is trust the username it returns. A name with a null
 * byte in it parses fine here and is refused by {@link validateUsername}
 * before it reaches an operating system, which is where that check belongs.
 */

import { pyInt } from "../pycompat/index.ts";

/** The two events a PAM module sends. */
export const PAM_EVENTS = ["open", "close"] as const;

/** One of {@link PAM_EVENTS}. */
export type PamEventName = (typeof PAM_EVENTS)[number];

/** What a session does with the terminal it is told about. */
export type PamMode = "notify" | "capture";

/** One PAM notification, once it has been believed. */
export interface PamEvent {
  event: PamEventName;
  username: string;
  tty: string;
  pid: number;
  mode: PamMode;
  /** Where to reach the captured terminal, when there is one. */
  captureSocket: string | undefined;
}

/**
 * How long a line may be before it is dropped.
 *
 * A runaway sender is cut off at the line rather than at the connection: the
 * next line may be a real login.
 */
export const MAX_PAM_LINE = 4096;

/** The mode the notify socket is created with, and nothing wider. */
export const PAM_SOCKET_MODE = 0o600;

/**
 * The umask the socket is bound under.
 *
 * Bound under it rather than chmod'd after: a post-bind `chmod` leaves a
 * window in which the socket exists at the default mode and any local user can
 * connect to it.
 */
export const PAM_BIND_UMASK = 0o177;

/**
 * Read one notification line, or nothing when it cannot be believed.
 *
 * **A recorded divergence.** The reference documents itself as returning
 * nothing on any error, and for a line that is valid JSON but not an *object*
 * that is not what happens: it calls `.get` on a list, a string or a null and
 * raises, and the exception leaves the connection handler — so one such line
 * from any sender past the socket's permission gate ends that connection. This
 * returns nothing instead, which is what the reference says it does and what
 * every other malformed line here already gets.
 *
 * @param line The bytes as they came off the socket.
 */
export function parsePamEvent(line: Uint8Array): PamEvent | undefined {
  let data: unknown;
  try {
    // Decoded leniently on purpose: bytes that are not valid text still have
    // to be *read* before they can be refused, and a decoder that threw would
    // make a malformed line indistinguishable from a closed socket.
    // Not trimmed first: `JSON.parse` already ignores surrounding
    // whitespace, which is what the reference's `strip()` was for.
    data = JSON.parse(new TextDecoder().decode(line));
  } catch {
    return undefined;
  }
  // A list is not excluded separately: JSON cannot give one an `event`, so
  // it falls out at the next check like any other line that does not name one.
  if (typeof data !== "object" || data === null) {
    return undefined;
  }

  const event = (data as Record<string, unknown>).event;
  if (event !== "open" && event !== "close") {
    return undefined;
  }

  const username = pyStrOrEmpty((data as Record<string, unknown>).username);
  if (username === "") {
    return undefined;
  }

  return {
    event,
    username,
    tty: pyStrOrEmpty((data as Record<string, unknown>).tty),
    // Advisory, so a value that is not a number is zero rather than a
    // dropped event.
    pid: pyInt((data as Record<string, unknown>).pid ?? 0) ?? 0,
    // Narrowed rather than trusted: only this exact word reaches capture.
    mode: (data as Record<string, unknown>).mode === "capture" ? "capture" : "notify",
    captureSocket: pyTruthyString((data as Record<string, unknown>).capture_socket),
  };
}

/**
 * Whether a peer that connected may be believed.
 *
 * A peer whose uid could not be determined is *allowed*: the socket's 0600
 * mode is the baseline, and a platform without `SO_PEERCRED` — macOS, for one
 * — would otherwise have no working sessions at all. An allowlist, where an
 * operator set one, is enforced exactly.
 *
 * @param peerUid The connecting peer's uid, or nothing where the platform
 *   cannot say.
 * @param allowed The uids an operator permitted, or nothing to permit any.
 */
export function pamPeerAllowed(peerUid: number | undefined, allowed?: readonly number[]): boolean {
  if (peerUid === undefined) {
    return true;
  }
  if (allowed === undefined) {
    return true;
  }
  return allowed.includes(peerUid);
}

/**
 * Whether a line is short enough to read.
 *
 * A line over the cap is dropped on its own; the connection stays up because
 * the next line may be a real login.
 */
export function pamLineAcceptable(line: Uint8Array): boolean {
  return line.length <= MAX_PAM_LINE;
}

/** A value as `str(x or "")` gives it: a null or a false becomes empty. */
function pyStrOrEmpty(value: unknown): string {
  if (value === undefined || value === null || value === false || value === "" || value === 0) {
    return "";
  }
  return String(value);
}

/** A value as `str(x) if x else None` gives it. */
function pyTruthyString(value: unknown): string | undefined {
  if (value === undefined || value === null || value === false || value === "" || value === 0) {
    return undefined;
  }
  return String(value);
}
