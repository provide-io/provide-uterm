//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * ANSI colour code conversion for BBS terminal output.
 *
 * A pluggable dialect registry converts BBS colour tokens to standard ANSI
 * escape sequences, alongside colour-upgrade utilities (16-colour to
 * 256-colour or truecolor).
 *
 * Built-in dialects: extended tokens (`{F#}`/`{B#}`/`{P#}`/`{T#}`), TWGS
 * brace tokens (`{+c}`/`{-x}`/`{+Bw}`/`{NK}`), tilde codes (`~N`) and pipe
 * codes (`|00`-`|23`). Additional dialects can be registered at runtime via
 * {@link registerColorDialect}.
 *
 * Port of the Python modules `provide.uterm.ansi` and
 * `provide.uterm._ansi_dialects`, and the Go package `ansi`.
 */

export {
  emitColor,
  handleBraceTokens,
  handleExtendedTokens,
  handlePipeCodes,
  handleTildeCodes,
} from "./dialects.ts";
export { BOLD, CLEAR_SCREEN, color256ToRgb, DEFAULT_PALETTE, DEFAULT_RGB, RESET } from "./palette.ts";
export {
  type ColorDialectHandler,
  normalizeColors,
  previewAnsi,
  registerColorDialect,
  registeredDialects,
  unregisterColorDialect,
} from "./registry.ts";
export { upgradeTo256, upgradeToTruecolor } from "./upgrade.ts";
