//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Generic screen parsing utilities for BBS terminals.
 *
 * Port of the Python module `provide.uterm.screen` and the Go package
 * `screen`.
 *
 * - `cp437.ts` — the CP437 (DOS OEM) codec and its table.
 * - `parse.ts` — ANSI stripping and the screen extractors.
 */

export { CP437_TABLE, decodeCp437, encodeCp437 } from "./cp437.ts";
export {
  cleanScreenForDisplay,
  extractActionTags,
  extractKeyValuePairs,
  extractMenuOptions,
  extractNumberedList,
  normalizeTerminalText,
  stripAnsi,
} from "./parse.ts";
