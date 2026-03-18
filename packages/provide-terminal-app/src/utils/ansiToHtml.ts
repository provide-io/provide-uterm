//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Convert a string containing ANSI SGR escape sequences into an array of
 * HTML-safe segments with optional inline styles.  Supports:
 *   - Standard 8 foreground/background colors (30-37, 40-47)
 *   - Bright foreground/background colors (90-97, 100-107)
 *   - 256-color mode (38;5;n / 48;5;n)
 *   - 24-bit true-color mode (38;2;r;g;b / 48;2;r;g;b)
 *   - Bold (1), dim (2), italic (3), underline (4), inverse (7)
 *   - Reset (0)
 */

const COLORS_16: readonly string[] = [
  "#000000", "#cc0000", "#00cc00", "#cccc00",
  "#3465a4", "#cc00cc", "#06989a", "#d3d7cf",
  "#555753", "#ef2929", "#8ae234", "#fce94f",
  "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec",
];

interface Segment {
  text: string;
  style: string;
}

function color256(n: number): string {
  if (n < 16) return COLORS_16[n] ?? "#000";
  if (n < 232) {
    const idx = n - 16;
    const r = Math.floor(idx / 36) * 51;
    const g = Math.floor((idx % 36) / 6) * 51;
    const b = (idx % 6) * 51;
    return `rgb(${r},${g},${b})`;
  }
  const gray = 8 + (n - 232) * 10;
  return `rgb(${gray},${gray},${gray})`;
}

export function ansiToSegments(input: string): Segment[] {
  const segments: Segment[] = [];
  let fg = "";
  let bg = "";
  let bold = false;
  let dim = false;
  let italic = false;
  let underline = false;
  let inverse = false;

  // biome-ignore lint/suspicious/noControlCharactersInRegex: ANSI CSI parsing
  const parts = input.split(/\x1b\[([0-9;]*)m/);

  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 0) {
      // Text segment
      const text = parts[i] ?? "";
      if (text.length === 0) continue;
      const styleParts: string[] = [];
      const fgActual = inverse ? bg : fg;
      const bgActual = inverse ? fg : bg;
      if (fgActual) styleParts.push(`color:${fgActual}`);
      if (bgActual) styleParts.push(`background:${bgActual}`);
      if (bold) styleParts.push("font-weight:bold");
      if (dim) styleParts.push("opacity:0.6");
      if (italic) styleParts.push("font-style:italic");
      if (underline) styleParts.push("text-decoration:underline");
      segments.push({ text, style: styleParts.join(";") });
    } else {
      // SGR parameter string
      const raw = parts[i] ?? "";
      const params = raw.split(";").map(Number);
      for (let j = 0; j < params.length; j++) {
        const p = params[j] ?? 0;
        if (p === 0) { fg = ""; bg = ""; bold = false; dim = false; italic = false; underline = false; inverse = false; }
        else if (p === 1) bold = true;
        else if (p === 2) dim = true;
        else if (p === 3) italic = true;
        else if (p === 4) underline = true;
        else if (p === 7) inverse = true;
        else if (p === 22) { bold = false; dim = false; }
        else if (p === 23) italic = false;
        else if (p === 24) underline = false;
        else if (p === 27) inverse = false;
        else if (p === 39) fg = "";
        else if (p === 49) bg = "";
        else if (p >= 30 && p <= 37) fg = COLORS_16[p - 30] ?? "";
        else if (p >= 40 && p <= 47) bg = COLORS_16[p - 40] ?? "";
        else if (p >= 90 && p <= 97) fg = COLORS_16[p - 90 + 8] ?? "";
        else if (p >= 100 && p <= 107) bg = COLORS_16[p - 100 + 8] ?? "";
        else if (p === 38 && (params[j + 1] ?? 0) === 5) { fg = color256(params[j + 2] ?? 0); j += 2; }
        else if (p === 48 && (params[j + 1] ?? 0) === 5) { bg = color256(params[j + 2] ?? 0); j += 2; }
        else if (p === 38 && (params[j + 1] ?? 0) === 2) { fg = `rgb(${params[j + 2] ?? 0},${params[j + 3] ?? 0},${params[j + 4] ?? 0})`; j += 4; }
        else if (p === 48 && (params[j + 1] ?? 0) === 2) { bg = `rgb(${params[j + 2] ?? 0},${params[j + 3] ?? 0},${params[j + 4] ?? 0})`; j += 4; }
      }
    }
  }

  return segments;
}

/** Strip all ANSI escape sequences from a string. */
export function stripAnsi(input: string): string {
  // biome-ignore lint/suspicious/noControlCharactersInRegex: ANSI CSI stripping
  return input.replace(/\x1b\[[0-9;]*m/g, "");
}
