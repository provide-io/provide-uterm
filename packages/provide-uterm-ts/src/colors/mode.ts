//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Unified color-mode dispatcher — handles strings or bytes transparently.
 *
 * Port of the Python module `provide.uterm.colors.mode`.
 */

import { downgradeTo16, downgradeTo256 } from "./downgrade.ts";

/** Color-mode filter selector. */
export type ColorMode = "passthrough" | "256" | "16";

/**
 * Apply a color-mode filter to ANSI text containing SGR sequences.
 *
 * Modes other than `"passthrough"` and `"256"` downgrade to the base
 * 16-color palette, mirroring the Python `Literal` contract.
 */
export function applyColorMode(data: string, mode: ColorMode): string {
  if (mode === "passthrough") {
    return data;
  }
  return mode === "256" ? downgradeTo256(data) : downgradeTo16(data);
}

/**
 * Apply a color-mode filter to raw bytes containing SGR sequences.
 *
 * The Python implementation decodes bytes as latin-1, runs the SGR regex,
 * and re-encodes. Latin-1 maps bytes 0-255 one-to-one onto code points 0-255
 * and both the SGR pattern and every replacement it emits are pure ASCII, so
 * the round-trip is byte-for-byte faithful.
 */
export function applyColorModeBytes(data: Uint8Array, mode: ColorMode): Uint8Array {
  if (mode === "passthrough") {
    return data;
  }
  const text = Buffer.from(data).toString("latin1");
  const filtered = mode === "256" ? downgradeTo256(text) : downgradeTo16(text);
  return new Uint8Array(Buffer.from(filtered, "latin1"));
}
