//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The cell model and colour tables.
 *
 * Port of the observable behaviour of pyte's `Char` and `graphics` modules.
 * pyte is LGPL and none of it is copied here: the tables were exported from a
 * running pyte by `testdata/gen_vt_golden.py` and the corpus asserts them.
 */

/** One screen cell: its character and every rendition attribute. */
export interface Char {
  data: string;
  fg: string;
  bg: string;
  bold: boolean;
  italics: boolean;
  underscore: boolean;
  reverse: boolean;
  strikethrough: boolean;
  blink: boolean;
}

/** A blank cell with every attribute at its default. */
export function defaultChar(): Char {
  return {
    data: " ",
    fg: "default",
    bg: "default",
    bold: false,
    italics: false,
    underscore: false,
    reverse: false,
    strikethrough: false,
    blink: false,
  };
}

/** Whether `char` is indistinguishable from a blank default cell. */
export function isDefaultChar(char: Char): boolean {
  return (
    char.data === " " &&
    char.fg === "default" &&
    char.bg === "default" &&
    !char.bold &&
    !char.italics &&
    !char.underscore &&
    !char.reverse &&
    !char.strikethrough &&
    !char.blink
  );
}

/** SGR codes that set a foreground colour by name. */
export const FG_ANSI: Readonly<Record<number, string>> = {
  30: "black",
  31: "red",
  32: "green",
  33: "brown",
  34: "blue",
  35: "magenta",
  36: "cyan",
  37: "white",
  39: "default",
};

/** SGR codes that set a background colour by name. */
export const BG_ANSI: Readonly<Record<number, string>> = {
  40: "black",
  41: "red",
  42: "green",
  43: "brown",
  44: "blue",
  45: "magenta",
  46: "cyan",
  47: "white",
  49: "default",
};

/** SGR codes for the bright (AIX) foreground colours. */
export const FG_AIXTERM: Readonly<Record<number, string>> = {
  90: "brightblack",
  91: "brightred",
  92: "brightgreen",
  93: "brightbrown",
  94: "brightblue",
  95: "brightmagenta",
  96: "brightcyan",
  97: "brightwhite",
};

/** SGR codes for the bright (AIX) background colours. */
export const BG_AIXTERM: Readonly<Record<number, string>> = {
  100: "brightblack",
  101: "brightred",
  102: "brightgreen",
  103: "brightbrown",
  104: "brightblue",
  105: "brightmagenta",
  106: "brightcyan",
  107: "brightwhite",
};

/** SGR codes that toggle a boolean attribute, and the attribute they set. */
export const TEXT_ATTRIBUTES: Readonly<Record<number, { field: keyof Char; value: boolean }>> = {
  1: { field: "bold", value: true },
  3: { field: "italics", value: true },
  4: { field: "underscore", value: true },
  5: { field: "blink", value: true },
  7: { field: "reverse", value: true },
  9: { field: "strikethrough", value: true },
  22: { field: "bold", value: false },
  23: { field: "italics", value: false },
  24: { field: "underscore", value: false },
  25: { field: "blink", value: false },
  27: { field: "reverse", value: false },
  29: { field: "strikethrough", value: false },
};

/** Component levels of the 6x6x6 colour cube. */
const CUBE_LEVELS = [0x00, 0x5f, 0x87, 0xaf, 0xd7, 0xff] as const;

/** pyte's own base sixteen, which differ from the uterm BBS palette. */
const BASE_16: readonly string[] = [
  "000000",
  "cd0000",
  "00cd00",
  "cdcd00",
  "0000ee",
  "cd00cd",
  "00cdcd",
  "e5e5e5",
  "7f7f7f",
  "ff0000",
  "00ff00",
  "ffff00",
  "5c5cff",
  "ff00ff",
  "00ffff",
  "ffffff",
];

/** Render one byte as two lowercase hex digits. */
function hexByte(value: number): string {
  return value.toString(16).padStart(2, "0");
}

/** Build the 256-entry index-to-hex table pyte resolves `38;5;N` against. */
function buildFgBg256(): string[] {
  const table = [...BASE_16];
  for (const r of CUBE_LEVELS) {
    for (const g of CUBE_LEVELS) {
      for (const b of CUBE_LEVELS) {
        table.push(`${hexByte(r)}${hexByte(g)}${hexByte(b)}`);
      }
    }
  }
  for (let i = 0; i < 24; i += 1) {
    const level = hexByte(8 + i * 10);
    table.push(`${level}${level}${level}`);
  }
  return table;
}

/** Index-to-hex table for the 256-colour SGR forms. */
export const FG_BG_256: readonly string[] = buildFgBg256();
