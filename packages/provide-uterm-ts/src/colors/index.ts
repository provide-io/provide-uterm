//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Color-downgrade utilities: truecolor SGR → 256-color / 16-color.
 *
 * Port of the Python package `provide.uterm.colors` and the Go package
 * `colors`. Each concern lives in its own tight module:
 *
 * - `rgb.ts` — RGB-to-palette-index mapping (`rgbTo256`, `rgbTo16Index`).
 * - `sgr.ts` — SGR parameter-list rewriting.
 * - `downgrade.ts` — text-level `downgradeTo256` / `downgradeTo16`.
 * - `mode.ts` — unified `applyColorMode` / `applyColorModeBytes` dispatchers.
 */

export { downgradeTo16, downgradeTo256 } from "./downgrade.ts";
export { type ColorMode, applyColorMode, applyColorModeBytes } from "./mode.ts";
export { rgbTo16Index, rgbTo256 } from "./rgb.ts";
export { type DowngradeTarget, rewriteParams, sgrPattern } from "./sgr.ts";
