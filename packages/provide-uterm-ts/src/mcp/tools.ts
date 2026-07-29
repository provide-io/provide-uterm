//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The checks the MCP tools make on their own arguments, and what a model sees.
 *
 * Port of the validators in `provide.uterm.ai.server_validators` that the tool
 * bodies call.
 *
 * `session_create` is how a model starts a process, so its arguments are
 * vetted before anything is spawned. The part worth spelling out: the host
 * inside a URL is checked as well as one given beside it, because a model that
 * cannot pass `host: "127.0.0.1"` could otherwise pass
 * `url: "ws://127.0.0.1/"` and reach the same place. And the scheme is an
 * allowlist, so `file://` and `javascript:` are refused rather than handed to
 * whatever would open them.
 *
 * The other half is what a model is *shown*. Two of the three output shapes
 * strip ANSI: a model reading escape sequences is reading noise, and one being
 * fed them is a prompt-injection surface.
 */

import { stripAnsi } from "../screen/index.ts";
import { isAllowedConnector, isInternalHost } from "./policy.ts";

/** The URL schemes a session may be pointed at. */
export const ALLOWED_URL_SCHEMES: ReadonlySet<string> = new Set(["ws", "wss", "http", "https", "telnet", "ssh"]);

/** The lowest and highest a TCP port may be. */
export const MIN_PORT = 1;
export const MAX_PORT = 65535;

/** How a snapshot may be shaped for a model. */
export type SnapshotOutput = "raw" | "text" | "rendered";

/** Why a `session_create` was refused, in the shape every tool answers with. */
export type SessionCreateRejection =
  | { success: false; error: "invalid_connector_type"; connector_type: string }
  | { success: false; error: "invalid_port"; port: number }
  | { success: false; error: "invalid_url_scheme"; scheme: string }
  | { success: false; error: "invalid_host"; host: string };

/** What a model asked to start. */
export interface SessionCreateRequest {
  connectorType: string;
  url?: string | undefined;
  port?: number | undefined;
  host?: string | undefined;
}

/**
 * Vet a `session_create` before anything is spawned.
 *
 * Checked in the reference's order — connector, port, URL, host — so a request
 * wrong in more than one way reports the same thing on both sides.
 *
 * @returns Nothing when the request is acceptable, or why it was refused.
 */
export function validateSessionCreate(request: SessionCreateRequest): SessionCreateRejection | undefined {
  if (!isAllowedConnector(request.connectorType)) {
    return { success: false, error: "invalid_connector_type", connector_type: request.connectorType };
  }
  if (request.port !== undefined && (request.port < MIN_PORT || request.port > MAX_PORT)) {
    return { success: false, error: "invalid_port", port: request.port };
  }
  if (request.url !== undefined) {
    const scheme = request.url.includes("://") ? (request.url.split("://", 1)[0] as string).toLowerCase() : "";
    if (!ALLOWED_URL_SCHEMES.has(scheme)) {
      // An empty scheme is named rather than reported as blank, so an
      // operator reading the refusal can tell "no scheme" from "a scheme I
      // have not heard of".
      return { success: false, error: "invalid_url_scheme", scheme: scheme === "" ? "<missing>" : scheme };
    }
    const host = urlHost(request.url);
    // The host *inside* the URL, checked separately: a model that cannot pass
    // an internal host beside the URL could otherwise pass one within it.
    if (host !== undefined && isInternalHost(host)) {
      return { success: false, error: "invalid_host", host };
    }
  }
  if (request.host !== undefined && isInternalHost(request.host)) {
    return { success: false, error: "invalid_host", host: request.host };
  }
  return undefined;
}

/**
 * Trim a screen to its last few lines.
 *
 * A screen shorter than the limit is returned as it was — including its
 * trailing newline, which splitting and rejoining would eat.
 */
export function trimTail(screen: string, tailLines: number | undefined): string {
  if (tailLines === undefined || tailLines <= 0) {
    return screen;
  }
  const lines = splitLines(screen);
  if (lines.length <= tailLines) {
    return screen;
  }
  return lines.slice(-tailLines).join("\n");
}

/**
 * Shape a snapshot for a model.
 *
 * `raw` hands back everything the server said. `text` is the screen and
 * nothing else. `rendered` keeps the visual grid and the layout a model needs
 * to reason about it — where the cursor is, and how big the screen is.
 *
 * The first keeps its escape sequences and the other two do not, which is the
 * decision here: a model reading escapes is reading noise, and one being fed
 * them is a prompt-injection surface. Anything that is not `raw` or `text` is
 * treated as `rendered`, so an unknown mode gets the safe shape rather than
 * the unfiltered one.
 */
export function cleanSnapshot(
  snapshot: Readonly<Record<string, unknown>>,
  output: string,
  tailLines?: number,
): Record<string, unknown> {
  const screen = typeof snapshot.screen === "string" ? snapshot.screen : "";
  if (output === "raw") {
    const trimmed = trimTail(screen, tailLines);
    return trimmed === screen ? { ...snapshot } : { ...snapshot, screen: trimmed };
  }
  const cleaned = trimTail(stripAnsi(screen), tailLines);
  if (output === "text") {
    return { screen: cleaned };
  }
  const result: Record<string, unknown> = { screen: cleaned };
  for (const key of ["cursor", "cols", "rows"]) {
    if (key in snapshot) {
      result[key] = snapshot[key];
    }
  }
  return result;
}

/**
 * The host a URL names, exactly as it was written.
 *
 * Not `new URL(...).hostname`, which *normalises*: JavaScript turns
 * `ws://2130706433/` into `127.0.0.1` where Python leaves it alone. Both
 * refuse the request either way — {@link isInternalHost} understands the
 * numeric forms — but the refusal names a host, and it should name the one the
 * caller actually sent rather than what a URL parser made of it.
 */
function urlHost(url: string): string | undefined {
  // Always matches by the time this runs: the scheme check above has already
  // established that the URL begins with one of the permitted schemes and a
  // separator, and refused it otherwise.
  const match = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/([^/?#]*)/.exec(url) as RegExpExecArray;
  const authority = match[1] as string;
  const afterUserinfo = authority.slice(authority.lastIndexOf("@") + 1);
  const host = afterUserinfo.startsWith("[")
    ? afterUserinfo.slice(1, afterUserinfo.indexOf("]"))
    : (afterUserinfo.split(":")[0] as string);
  return host === "" ? undefined : host;
}

/** Split as Python's `str.splitlines()` does, dropping a trailing break. */
function splitLines(text: string): string[] {
  const lines = text.split(/\r\n|\r|\n/);
  if (lines[lines.length - 1] === "") {
    lines.pop();
  }
  return lines;
}
