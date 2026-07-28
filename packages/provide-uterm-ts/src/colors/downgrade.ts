//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Text-level color downgrade functions.
 *
 * These are the downgrade counterparts to `upgradeTo256` /
 * `upgradeToTruecolor` in the `ansi` module. Both operate on string input /
 * output; the unified string / bytes dispatcher lives in `mode.ts`.
 *
 * Port of the Python module `provide.uterm.colors.downgrade`.
 */

import { type DowngradeTarget, rewriteParams, sgrPattern } from "./sgr.ts";

/** Apply {@link rewriteParams} to every SGR sequence in `text`. */
function substitute(text: string, mode: DowngradeTarget): string {
  return text.replace(sgrPattern(), (_match, params: string) => rewriteParams(params, mode));
}

/**
 * Downgrade truecolor SGR sequences in text to xterm-256 cube codes.
 *
 * Replaces `\x1b[38;2;R;G;Bm` and `\x1b[48;2;R;G;Bm` runs within SGR
 * parameter lists with their nearest xterm-256 palette index equivalents
 * (`38;5;N` / `48;5;N`). Non-truecolor SGR and other escape sequences pass
 * through unchanged. Idempotent on content that contains no truecolor.
 */
export function downgradeTo256(text: string): string {
  return substitute(text, "256");
}

/**
 * Downgrade truecolor SGR sequences in text to base 16-color codes.
 *
 * Uses Euclidean-nearest matching over the canonical BBS 16-color palette.
 * Non-truecolor SGR passes through unchanged.
 */
export function downgradeTo16(text: string): string {
  return substitute(text, "16");
}
