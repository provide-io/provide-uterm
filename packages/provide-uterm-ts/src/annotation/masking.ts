//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Masking credential-shaped text before a match reaches a description.
 *
 * Port of the Python module `provide.uterm.annotation._masking`; the pattern
 * strings are identical and `annotation_golden.json` pins them.
 *
 * THE RULE: a template that names `{match}` receives `maskSecrets(fullMatch)`
 * cut to 80 characters -- masked FIRST, so the cut cannot leave the head of a
 * credential behind as a fragment no redaction rule recognises. The masks run
 * in order, each over the output of the one before. Every mask has three
 * groups -- kept prefix, secret, kept suffix -- and every non-overlapping
 * match, left to right, becomes prefix + {@link SECRET_MARKER} + suffix. The
 * patterns use ASCII-only classes, no `\s` and no case-insensitive flag, and no
 * lookaround, so CPython, RE2, ECMAScript and .NET agree on every input.
 * Detection and the match key are unaffected.
 */

/** What every credential-shaped substring of a match becomes. */
export const SECRET_MARKER = "[SECRET]";

/** The masks, in the order they are applied, as the reference writes them. */
export const SECRET_MASK_PATTERNS: readonly string[] = [
  String.raw`([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn][ \t]*:[ \t]*)([^'"\r\n]+)()`,
  String.raw`((?:[Bb][Ee][Aa][Rr][Ee][Rr]|[Bb][Aa][Ss][Ii][Cc])[ \t\n\r\f\v]+)([^ \t\n\r\f\v'"]+)()`,
  String.raw`((?:[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]` +
    String.raw`|[Tt][Oo][Kk][Ee][Nn]|[Aa][Pp][Ii][_-]?[Kk][Ee][Yy])['"]?[ \t]*[=:][ \t]*['"]?)` +
    String.raw`([^ \t\n\r\f\v'"]+)()`,
  String.raw`((?:^|[ \t])(?:-[uU][ \t]*|--(?:user|proxy-user|password|http-password|ftp-password|proxy-password)` +
    String.raw`(?:[ \t]+|=))['"]?)([^ \t\n\r\f\v'"]+)()`,
  String.raw`(://)([^/ \t\n\r\f\v@:'"]*:[^/ \t\n\r\f\v@'"]*)(@)`,
  String.raw`()(AKIA[0-9A-Z]{12,})()`,
  String.raw`()(gh[psourx]_[A-Za-z0-9_]{8,})()`,
  String.raw`()(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)()`,
  String.raw`(-----BEGIN[ \t]+[A-Z ]*PRIVATE KEY-----)([^-]+)()`,
];

const SECRET_MASKS: readonly RegExp[] = SECRET_MASK_PATTERNS.map((source) => new RegExp(source, "g"));

/** `text` with every credential-shaped substring replaced by {@link SECRET_MARKER}. */
export function maskSecrets(text: string): string {
  let masked = text;
  for (const mask of SECRET_MASKS) {
    masked = masked.replace(mask, (_whole, prefix: string, _secret: string, suffix: string) => {
      return prefix + SECRET_MARKER + suffix;
    });
  }
  return masked;
}
