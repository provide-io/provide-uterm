//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * `uterm inspect` — putting a proxy in front of a local port and sharing it.
 *
 * Port of `provide.uterm.cli.inspect`'s decisions. What is settled before
 * anything is proxied:
 *
 * * **Where the WebSocket is**, resolved from whatever the server answered.
 * * **What the session is called**, which defaults to the port being
 *   inspected rather than to nothing.
 * * **What is printed** — the share link is the whole output, and a caller
 *   who has turned interception on has to be told, because a proxy that
 *   pauses requests looks exactly like one that has hung.
 */

import { resolveWsEndpoint } from "./share.ts";

/** What the caller asked to inspect. */
export interface InspectArgs {
  server: string;
  /** The local port whose traffic is proxied. */
  port: number;
  /** Where the proxy listens. Zero means whichever is free. */
  listenPort?: number | undefined;
  displayName?: string | undefined;
  intercept?: unknown;
  interceptTimeout?: number | undefined;
  interceptTimeoutAction?: string | undefined;
}

/** What the tunnel server answered. */
export interface InspectTunnelInfo {
  tunnel_id?: unknown;
  session_id?: unknown;
  share_url?: unknown;
  ws_endpoint?: unknown;
  worker_token?: unknown;
}

/** How long a paused request waits unless somebody says otherwise. */
export const DEFAULT_INTERCEPT_TIMEOUT_S = 30;

/** What happens to a paused request nobody answers. */
export const DEFAULT_INTERCEPT_TIMEOUT_ACTION = "forward";

/** What the command decided to do. */
export type InspectPlan =
  | {
      ok: true;
      /** Lines written to standard output, in order. */
      output: string[];
      wsEndpoint: string;
      workerToken: string;
      displayName: string;
      targetPort: number;
      listenPort: number;
      intercept: boolean;
      interceptTimeout: number;
      interceptTimeoutAction: string;
    }
  | { ok: false; output: string[]; error: string; exitCode: number };

/** What a session is called when nobody named it. */
export function inspectDisplayName(args: InspectArgs): string {
  // The port rather than nothing: a list of shares all called the same thing
  // is a list nobody can read.
  return args.displayName !== undefined && args.displayName !== "" ? args.displayName : `http:${args.port}`;
}

/** Read a string field, or empty when it is absent or the wrong type. */
function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * Work out what inspecting would do.
 *
 * Separated from doing it so the decisions can be checked without a network
 * or a proxy.
 */
export function planInspect(args: InspectArgs, info: InspectTunnelInfo): InspectPlan {
  const endpoint = text(info.ws_endpoint);
  const shareUrl = text(info.share_url);
  // Either name: a server that calls it a session and one that calls it a
  // tunnel are both naming the thing that was just created. Chosen on whether
  // the field is *there*, not on whether it is empty — the reference reads it
  // with a default, so a server that sent an empty `tunnel_id` has named the
  // tunnel as nothing rather than deferred to `session_id`.
  const tunnelId = Object.hasOwn(info, "tunnel_id") ? text(info.tunnel_id) : text(info.session_id);

  // Printed before the endpoint is checked, as the reference prints it: the
  // tunnel was created either way, and saying so is what tells an operator
  // how far it got.
  const created = [`Creating tunnel... ${tunnelId !== "" ? `done (${tunnelId})` : "done"}`];

  if (endpoint === "") {
    return { ok: false, output: created, error: "error: server response missing ws_endpoint", exitCode: 1 };
  }

  // By truth, not identity: the reference reads the flag and then asks
  // `if intercept:`, so a caller passing 1 or a non-empty string has turned
  // it on.
  const intercept = Boolean(args.intercept);
  const interceptTimeout = args.interceptTimeout ?? DEFAULT_INTERCEPT_TIMEOUT_S;
  const interceptTimeoutAction = args.interceptTimeoutAction ?? DEFAULT_INTERCEPT_TIMEOUT_ACTION;

  const output = [...created, `Inspecting HTTP traffic on localhost:${args.port}`];
  if (shareUrl !== "") {
    output.push(`  Share: ${shareUrl}`);
  }
  if (intercept) {
    // Said out loud: a proxy that pauses requests looks exactly like one that
    // has hung.
    output.push(`  Intercept: ON (timeout: ${formatSeconds(interceptTimeout)}s, action: ${interceptTimeoutAction})`);
  }
  output.push("Press Ctrl+C to stop.\n");

  return {
    ok: true,
    output,
    wsEndpoint: resolveWsEndpoint(args.server, endpoint),
    workerToken: text(info.worker_token),
    displayName: inspectDisplayName(args),
    targetPort: args.port,
    listenPort: args.listenPort ?? 0,
    intercept,
    interceptTimeout,
    interceptTimeoutAction,
  };
}

/** A duration as Python renders the float it is, so the line reads the same. */
function formatSeconds(value: number): string {
  return Number.isInteger(value) ? `${value}.0` : String(value);
}
