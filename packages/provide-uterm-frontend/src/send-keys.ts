//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The take-control "Send keys" box: typed text with escapes for the keys a
 * text field cannot hold.
 *
 * `\r` (Return), `\n`, `\t`, `\e` and `\x1b` (Escape, or any `\xHH` byte) are
 * read as the keys they name, and `\\` as one backslash, so a literal `\r` can
 * still be sent as `\\r`. Anything else is sent as typed. One pass, left to
 * right: `\\r` is a backslash and an `r`, never a backslash and a Return.
 */

/** What the box's placeholder and tooltip say about the escapes. */
export const SEND_KEYS_HELP = "\\r Return, \\n, \\t, \\e or \\x1b Esc, \\xHH any byte, \\\\ a backslash";

const ESCAPE = /\\(x[0-9a-fA-F]{2}|[rnte\\])/g;

const NAMED: Readonly<Record<string, string>> = { r: "\r", n: "\n", t: "\t", e: "\x1b", "\\": "\\" };

/** Read the box's text as the keys to send. */
export function unescapeSendKeys(text: string): string {
  return text.replace(ESCAPE, (_match, code: string) =>
    code.length === 3 ? String.fromCharCode(Number.parseInt(code.slice(1), 16)) : (NAMED[code] as string),
  );
}
