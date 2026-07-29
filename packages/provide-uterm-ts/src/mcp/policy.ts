//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What an LLM is allowed to do, and where it may point a session.
 *
 * Port of `provide.uterm.ai.policy` and the admission checks in
 * `provide.uterm.ai.server_validators`. This is the chokepoint every MCP tool
 * call passes through, and three decisions are the whole of it.
 *
 * **Every tool names the role it needs, and an unknown tool raises.** That is
 * what stops a newly added tool from slipping through unguarded: the table is
 * the single source of truth, and a tool missing from it fails loudly rather
 * than defaulting to something permissive. A role nobody defined ranks *below*
 * viewer, so an unrecognised role is not accidentally an admin.
 *
 * **Which host a session may target.** Refused without resolving DNS —
 * rebinding and egress filtering are the server's job — and refused across
 * every numeric form a C resolver accepts, not just the dotted quad. An LLM
 * that cannot write `127.0.0.1` can write `2130706433`, and it reaches the
 * same place.
 *
 * **Which connectors may be spawned**, because `session_create` is how an LLM
 * starts a process.
 */

import { inetAton, ipAddress, isLinkLocal, isLoopback, isPrivate } from "../pycompat/index.ts";

/** The ladder, least privileged first. */
export const ROLES = ["viewer", "operator", "admin"] as const;

/** One of {@link ROLES}. */
export type Role = (typeof ROLES)[number];

/** Rank by privilege. A name nobody defined ranks below every real role. */
const ROLE_RANK: ReadonlyMap<string, number> = new Map([
  ["viewer", 0],
  ["operator", 1],
  ["admin", 2],
]);

/** How privileged a role is; unknown ranks below viewer. */
export function roleRank(role: string): number {
  return ROLE_RANK.get(role) ?? -1;
}

/**
 * Whether one role satisfies a requirement.
 *
 * An unrecognised role never does — including one that only differs in case,
 * so `Admin` is not `admin`.
 */
export function roleAtLeast(actual: string, minimum: Role): boolean {
  return roleRank(actual) >= roleRank(minimum);
}

/**
 * The minimum role each tool needs.
 *
 * Read-only inspection is viewer. Session lifecycle, input mode and
 * annotation are operator. Anything with a wide blast radius is admin:
 * hijacking a running worker, disconnecting one, broadcasting input to a
 * fanout group, or spawning an arbitrary connector.
 */
export const TOOL_REQUIRED_ROLES: ReadonlyMap<string, Role> = new Map([
  // Hijack lifecycle — exclusive worker takeover.
  ["hijack_begin", "admin"],
  ["hijack_heartbeat", "admin"],
  ["hijack_read", "operator"],
  ["hijack_send", "admin"],
  ["hijack_step", "admin"],
  ["hijack_release", "admin"],
  // Graphical hijack lifecycle.
  ["gui_hijack_begin", "admin"],
  ["gui_hijack_release", "admin"],
  ["gui_screenshot", "operator"],
  ["gui_click", "admin"],
  ["gui_type", "admin"],
  ["gui_key", "operator"],
  ["gui_drag", "operator"],
  // Read-only inspection.
  ["session_list", "viewer"],
  ["session_status", "viewer"],
  ["session_read", "viewer"],
  ["server_health", "viewer"],
  // Session lifecycle and mode.
  ["session_connect", "operator"],
  ["session_disconnect", "operator"],
  ["session_set_mode", "operator"],
  // Event streams, read-only.
  ["session_watch", "viewer"],
  ["session_subscribe", "viewer"],
  // Annotations write to the recording timeline.
  ["session_annotate", "operator"],
  // Making a group is configuration; broadcasting into one is not.
  ["fanout_group_create", "operator"],
  ["fanout_send", "admin"],
  // Spawning a process, forcing a worker's mode, disconnecting a worker.
  ["session_create", "admin"],
  ["worker_input_mode", "admin"],
  ["worker_disconnect", "admin"],
]);

/**
 * Tools that need a hijack lease as well as a role.
 *
 * A role says who may ask; a lease says that nobody else is currently driving.
 */
export const HIJACK_LEASE_REQUIRED_TOOLS: ReadonlySet<string> = new Set(["gui_key", "gui_drag"]);

/**
 * The minimum role for a tool.
 *
 * @throws {Error} When the tool has no entry — a programming error, and the
 *   reason it is loud: a tool with no policy must not run.
 */
export function requiredRole(tool: string): Role {
  const role = TOOL_REQUIRED_ROLES.get(tool);
  if (role === undefined) {
    throw new Error(`No authorization policy registered for MCP tool '${tool}'`);
  }
  return role;
}

/** The connectors an LLM may spawn. A closed set, by name. */
export const ALLOWED_CONNECTOR_TYPES: ReadonlySet<string> = new Set([
  "shell",
  "telnet",
  "ssh",
  "ws",
  "websocket",
  "pty",
]);

/** Whether this connector may be spawned through the tool surface. */
export function isAllowedConnector(connectorType: string): boolean {
  return ALLOWED_CONNECTOR_TYPES.has(connectorType);
}

/** How long a caller-supplied regex may be before it is refused outright. */
export const MAX_USER_PATTERN_LEN = 512;

/** How many bytes of keystrokes one send may carry. */
export const MAX_KEYSTROKE_BYTES = 4096;

/**
 * Whether a session may target a private or internal host.
 *
 * Deny by default: an LLM should not be able to pivot to 169.254.169.254,
 * to RFC1918, or to loopback. An operator who genuinely needs an internal
 * target opts in.
 */
export const ALLOW_PRIVATE_HOSTS = false;

/** Names that mean an internal endpoint whatever they resolve to. */
const BLOCKED_HOST_NAMES: ReadonlySet<string> = new Set(["localhost", "metadata.google.internal", "metadata"]);

/**
 * Whether this host is an internal or metadata endpoint.
 *
 * No DNS lookup happens here, deliberately: rebinding and egress control are
 * the server's, and a lookup in this path would be a blocking call in an
 * admission check. What this does instead is refuse every *written* form that
 * names something internal — including the numeric forms a C resolver accepts
 * and a dotted-quad blocklist does not.
 *
 * A genuine hostname is not refused here. It reaches the server's egress
 * guard, which resolves it and checks the address it actually got.
 *
 * @param allowPrivate Whether private, reserved and unspecified ranges are
 *   permitted. Loopback and link-local never are.
 */
export function isInternalHost(host: string, allowPrivate: boolean = ALLOW_PRIVATE_HOSTS): boolean {
  // A trailing root dot makes an FQDN of a name, and `localhost.` is
  // `localhost` — matching without stripping it lets the FQDN form past an
  // exact-match denylist.
  const candidate = host.trim().replaceAll("[", "").replaceAll("]", "").replace(/\.+$/, "").toLowerCase();
  if (BLOCKED_HOST_NAMES.has(candidate)) {
    return true;
  }
  // RFC 6761 reserves the whole subtree and requires it to resolve to
  // loopback, so `api.localhost` is as internal as `localhost`.
  if (candidate.endsWith(".localhost")) {
    return true;
  }

  // Not a canonical address: it may still be one of the numeric forms a
  // resolver takes. A real hostname yields nothing here and is left to the
  // server.
  const address = ipAddress(candidate) ?? inetAton(candidate);
  if (address === undefined) {
    return false;
  }
  if (isLoopback(address) || isLinkLocal(address)) {
    return true;
  }
  // The reference writes this as private-or-reserved-or-unspecified. Those
  // last two are subsets of the first in both runtimes — every reserved and
  // every unspecified address is also private — so one term says it, and the
  // corpus carries `240.0.0.1`, `255.255.255.255` and `::` to prove the three
  // still agree.
  return !allowPrivate && isPrivate(address);
}
