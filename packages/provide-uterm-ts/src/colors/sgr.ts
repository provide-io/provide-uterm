//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * SGR parameter-list rewriting for color downgrade.
 *
 * Given an SGR parameter list (the `N;M;...` part between `ESC [` and `m`),
 * scan for truecolor runs (`38;2;R;G;B` foreground or `48;2;R;G;B`
 * background) and replace them with the configured lower-palette
 * equivalent. Other SGR parameters (bold, italic, 256-color, etc.) pass
 * through unchanged.
 *
 * Port of the Python module `provide.uterm.colors.sgr`.
 */

import { rgbTo16Index, rgbTo256 } from "./rgb.ts";

/**
 * Target palette for {@link rewriteParams}. `"passthrough"` is not a valid
 * rewrite target — the dispatcher handles it before reaching here.
 */
export type DowngradeTarget = "256" | "16";

/**
 * Build the SGR escape-sequence pattern: `\x1b[` params `m`, where params is
 * a possibly-empty semicolon-separated parameter list.
 *
 * Returned fresh per call because a `g`-flagged RegExp carries mutable
 * `lastIndex` state that would leak between scans.
 */
export function sgrPattern(): RegExp {
  return /\x1b\[([0-9;]*)m/g;
}

/**
 * Base ANSI 16-color foreground escape codes, indexed by palette index 0-15
 * (0-7 are the normal colors, 8-15 the bright variants).
 */
const FG_16 = [30, 34, 32, 36, 31, 35, 33, 37, 90, 94, 92, 96, 91, 95, 93, 97];
/** Base ANSI 16-color background escape codes, indexed by palette index 0-15. */
const BG_16 = [40, 44, 42, 46, 41, 45, 43, 47, 100, 104, 102, 106, 101, 105, 103, 107];

/**
 * Report whether `s` is a non-empty run of ASCII decimal digits.
 *
 * Stands in for Python's `str.isdigit()`. The SGR pattern only ever yields
 * `[0-9;]`, so within the scanner the two agree exactly; on a direct call
 * with a non-digit the run is passed through instead of raising.
 */
function isDigits(s: string): boolean {
  if (s === "") {
    return false;
  }
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charCodeAt(i);
    if (c < 0x30 || c > 0x39) {
      return false;
    }
  }
  return true;
}

/**
 * Rewrite an SGR parameter list, downgrading any truecolor runs.
 *
 * Walks the `;`-separated parameters looking for the 5-parameter run
 * `38;2;R;G;B` (foreground truecolor) or `48;2;R;G;B` (background
 * truecolor). Each such run is replaced with its equivalent under the target
 * mode; everything else is preserved in place and order.
 *
 * @param params SGR parameter list without the leading `\x1b[` or trailing
 *   `m`. May be empty.
 * @param mode `"256"` to map to the xterm-256 cube, `"16"` to map to the
 *   base 16-color palette.
 * @returns A full SGR escape sequence (`\x1b[<rewritten>m`).
 */
export function rewriteParams(params: string, mode: DowngradeTarget): string {
  if (params === "") {
    return `\x1b[${params}m`;
  }
  const parts = params.split(";");
  const out: string[] = [];
  const n = parts.length;
  let i = 0;
  while (i < n) {
    const head = parts[i] as string;
    if (
      i + 4 < n &&
      (head === "38" || head === "48") &&
      parts[i + 1] === "2" &&
      isDigits(parts[i + 2] as string) &&
      isDigits(parts[i + 3] as string) &&
      isDigits(parts[i + 4] as string)
    ) {
      const r = Number(parts[i + 2]);
      const g = Number(parts[i + 3]);
      const b = Number(parts[i + 4]);
      const isFg = head === "38";
      if (mode === "256") {
        out.push(isFg ? "38" : "48", "5", String(rgbTo256(r, g, b)));
      } else {
        const idx = rgbTo16Index(r, g, b);
        out.push(String((isFg ? FG_16[idx] : BG_16[idx]) as number));
      }
      i += 5;
      continue;
    }
    out.push(head);
    i += 1;
  }
  return `\x1b[${out.join(";")}m`;
}
