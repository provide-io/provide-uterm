//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * `uterm watch` — reading the tunnel out of whatever somebody pasted.
 *
 * Port of `provide.uterm.cli.watch`'s argument handling. Somebody watching a
 * tunnel has a link in their clipboard, not an identifier, so the command
 * takes either. Pulling the wrong part out of an address means watching
 * somebody else's tunnel — or nothing at all — so which part is the
 * identifier is pinned exactly.
 */

/** The routes a tunnel identifier can appear in. */
const TUNNEL_IN_URL = /\/(?:app\/(?:inspect|session|operator)\/|s\/)([a-zA-Z0-9_-]+)/;

/**
 * The tunnel a value names.
 *
 * A bare identifier is taken as it stands. Only something that looks like an
 * address is searched, and only the part before any query — a tunnel named in
 * a query parameter is not the tunnel the path names.
 *
 * An address naming no tunnel comes back whole rather than empty, which is
 * the reference's behaviour: whatever was passed is then treated as the
 * identifier, and the server refuses it by name rather than the command
 * failing with nothing to say.
 */
export function extractTunnelId(value: string): string {
  if (!value.includes("://")) {
    return value;
  }
  const match = TUNNEL_IN_URL.exec(value.split("?")[0] as string);
  return match === null ? value : (match[1] as string);
}
