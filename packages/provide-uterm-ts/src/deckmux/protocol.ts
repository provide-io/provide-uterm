//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The DeckMux wire protocol.
 *
 * Port of the Python module `provide.uterm.deckmux._protocol`.
 *
 * DeckMux is several people watching one terminal, so these messages are what
 * each of them learns about the others.
 */

/** Someone's presence changed. */
export const MSG_PRESENCE_UPDATE = "presence_update";

/** Everything a joiner needs to draw the room. */
export const MSG_PRESENCE_SYNC = "presence_sync";

/** Someone left. */
export const MSG_PRESENCE_LEAVE = "presence_leave";

/** Control moved from one person to another. */
export const MSG_CONTROL_TRANSFER = "control_transfer";

/** Keystrokes typed by somebody who did not hold control. */
export const MSG_QUEUED_INPUT = "queued_input";

/** Somebody asked for control. */
export const MSG_CONTROL_REQUEST = "control_request";

/** Control is about to move on its own. */
export const MSG_AUTO_TRANSFER_WARNING = "auto_transfer_warning";

/** Why control moved. */
export type TransferReason = "handover" | "auto_idle" | "admin_takeover" | "lease_expired";

/** How queued keystrokes are treated when control arrives. */
export type KeystrokeQueueMode = "display" | "replay";

/** How a keystroke is drawn for the other participants. */
export const KEY_SYMBOLS: Readonly<Record<string, string>> = {
  "\x1b[A": "↑",
  "\x1b[B": "↓",
  "\x1b[C": "→",
  "\x1b[D": "←",
  "\r": "↵",
  "\n": "↵",
  "\t": "⇥",
  "\x7f": "⌫",
  "\x08": "⌫",
  "\x1b": "⎋",
};

/**
 * Render raw keystrokes as the symbols other participants see.
 *
 * A three-character escape is matched before its first character, or an arrow
 * key would render as an escape symbol followed by two stray letters — which
 * is what everybody else would believe was typed. A control character with no
 * symbol is dropped rather than drawn, since it has no visible form.
 */
export function encodeKeysDisplay(rawKeys: string): string {
  const out: string[] = [];
  let index = 0;
  while (index < rawKeys.length) {
    const triple = rawKeys.slice(index, index + 3);
    if (index + 2 < rawKeys.length && triple in KEY_SYMBOLS) {
      out.push(KEY_SYMBOLS[triple] as string);
      index += 3;
      continue;
    }
    const single = rawKeys[index] as string;
    if (single in KEY_SYMBOLS) {
      out.push(KEY_SYMBOLS[single] as string);
    } else if (single >= " ") {
      out.push(single);
    }
    index += 1;
  }
  return out.join("");
}

/** The optional fields a presence update may carry, in the order it carries them. */
const PRESENCE_UPDATE_FIELDS = [
  "scroll_line",
  "scroll_range",
  "total_lines",
  "selection",
  "pin",
  "typing",
  "queued_keys",
  "is_owner",
] as const;

/**
 * Build a presence update.
 *
 * Only the named optional fields travel: a browser that invented one would
 * otherwise have it re-broadcast to everybody else verbatim.
 */
export function makePresenceUpdate(
  userId: string,
  name: string,
  color: string,
  role: string,
  fields: Record<string, unknown> = {},
): Record<string, unknown> {
  const message: Record<string, unknown> = {
    type: MSG_PRESENCE_UPDATE,
    user_id: userId,
    name,
    color,
    role,
  };
  for (const field of PRESENCE_UPDATE_FIELDS) {
    if (Object.hasOwn(fields, field)) {
      message[field] = fields[field];
    }
  }
  return message;
}

/** Build the message a joiner is sent. */
export function makePresenceSync(
  users: Array<Record<string, unknown>>,
  config: Record<string, unknown>,
): Record<string, unknown> {
  return { type: MSG_PRESENCE_SYNC, users, config };
}

/** Build the message that says somebody left. */
export function makePresenceLeave(userId: string): Record<string, unknown> {
  return { type: MSG_PRESENCE_LEAVE, user_id: userId };
}

/** Build the message that says control moved. */
export function makeControlTransfer(
  fromUser: string,
  toUser: string,
  reason: TransferReason,
  queuedKeys = "",
): Record<string, unknown> {
  return {
    type: MSG_CONTROL_TRANSFER,
    from_user_id: fromUser,
    to_user_id: toUser,
    reason,
    queued_keys: queuedKeys,
  };
}
