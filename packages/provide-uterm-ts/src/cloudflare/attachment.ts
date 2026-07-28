//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Reading what a WebSocket connection is, from what was attached to it.
 *
 * Port of the attachment-reading half of the Python module
 * `provide.uterm.cloudflare.do.session_runtime.ws_helpers`.
 *
 * A Durable Object can be evicted and resumed with its sockets still open,
 * and when that happens the attachment is the only thing it knows about a
 * connection. It carries who they are and what they may do, so reading it is
 * an authorisation decision made without any of the context that produced it.
 *
 * The reference keeps these on a mixin because its runtime needs them bound
 * to the object; they are functions here, because nothing about them depends
 * on the session.
 */

/** What kind of connection this is. */
export type SocketKind = "browser" | "worker" | "raw";

/** What a browser connection may do. */
export type BrowserRole = "admin" | "operator" | "viewer";

/** A connection that can be asked what was attached to it. */
export interface AttachedSocket {
  deserializeAttachment(): unknown;
}

/** The three kinds of connection. */
const SOCKET_KINDS: ReadonlySet<string> = new Set(["browser", "worker", "raw"]);

/**
 * The three roles a browser may hold.
 *
 * `viewer` is both the least privileged and the fallback, so accepting it
 * explicitly changes no answer — a viewer that failed the test would be
 * refused down to a viewer. It is listed because it is a role somebody may
 * legitimately hold, not merely what happens when nothing is known.
 */
const BROWSER_ROLES: ReadonlySet<string> = new Set(["admin", "operator", "viewer"]);

/**
 * The attachment's fields.
 *
 * Split at most twice, so the last field keeps any colons of its own: a
 * session id containing one would otherwise make the role unreadable and
 * silently demote the connection.
 */
function fields(attachment: string): string[] {
  const first = attachment.indexOf(":");
  if (first === -1) {
    return [attachment];
  }
  const second = attachment.indexOf(":", first + 1);
  if (second === -1) {
    return [attachment.slice(0, first), attachment.slice(first + 1)];
  }
  return [attachment.slice(0, first), attachment.slice(first + 1, second), attachment.slice(second + 1)];
}

/** Whatever the connection has attached, or nothing if it cannot say. */
function read(socket: AttachedSocket): unknown {
  try {
    return socket.deserializeAttachment();
  } catch {
    // A connection resumed from hibernation may not be able to answer.
    return undefined;
  }
}

/** A role named by an attachment that is not a string. */
function namedRole(attachment: unknown): string | undefined {
  if (typeof attachment !== "object" || attachment === null) {
    return undefined;
  }
  const holder = attachment as { get?: (key: string) => unknown; role?: unknown };
  const fromGet = typeof holder.get === "function" ? holder.get("role") : undefined;
  const role = fromGet ?? holder.role;
  // A non-string is no role. It would fail the membership tests either way,
  // but this says the attachment named nothing rather than naming something
  // unrecognised.
  return typeof role === "string" ? role : undefined;
}

/**
 * What kind of connection this is.
 *
 * Defaults to a browser: the overwhelming majority are, and a connection
 * mistaken for a worker would be handed the session's output stream.
 */
export function socketKind(socket: AttachedSocket): SocketKind {
  const attachment = read(socket);
  if (typeof attachment === "string") {
    const kind = fields(attachment)[0] as string;
    if (SOCKET_KINDS.has(kind)) {
      return kind as SocketKind;
    }
  }
  const named = namedRole(attachment);
  if (named !== undefined && SOCKET_KINDS.has(named)) {
    return named as SocketKind;
  }
  return "browser";
}

/** Options for {@link browserRole}. */
export interface BrowserRoleOptions {
  /** The Worker's auth mode, which decides what an unreadable role means. */
  mode: string;
}

/**
 * What a browser connection may do.
 *
 * Fails closed. A connection whose role cannot be recovered is a viewer, not
 * whatever it was before — which is the post-hibernation case, where the
 * attachment is all that survived.
 *
 * The reference grants admin under the open auth modes. Those modes no longer
 * exist: the Worker configuration refuses anything but `jwt` at startup, so
 * that branch is unreachable in a Worker that started at all. It is kept
 * because this function can be called with any mode, and dropping it would
 * change what such a call means rather than removing something dead.
 */
export function browserRole(socket: AttachedSocket, options: BrowserRoleOptions): BrowserRole {
  const attachment = read(socket);
  if (typeof attachment === "string") {
    const role = fields(attachment)[1];
    if (role !== undefined && BROWSER_ROLES.has(role)) {
      return role as BrowserRole;
    }
  }
  return options.mode === "none" || options.mode === "dev" ? "admin" : "viewer";
}

/**
 * Which session a connection belongs to.
 *
 * Falls back to the object's own session, which is right for a connection
 * that predates the field and for one whose attachment cannot be read.
 */
export function socketWorkerId(socket: AttachedSocket, fallback: string): string {
  const attachment = read(socket);
  if (typeof attachment === "string") {
    const workerId = fields(attachment)[2];
    if (workerId !== undefined && workerId !== "") {
      return workerId;
    }
  }
  return fallback;
}

/** Build the attachment for a connection, so it survives hibernation. */
export function encodeAttachment(kind: SocketKind, role: BrowserRole, workerId: string): string {
  return `${kind}:${role}:${workerId}`;
}
