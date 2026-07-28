//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Rendering an image as terminal art.
 *
 * Port of `provide.uterm.render.image` and `provide.uterm.render.sgr`.
 *
 * Two pixel rows per terminal row, using the lower-half block: the bottom
 * pixel is the foreground and the top one the background, so one cell carries
 * two pixels and a terminal renders an image at twice its apparent height.
 *
 * Decoding the image itself is not here — the reference takes that from PIL,
 * and this takes pixels from whatever the caller has.
 */

import { nearest16, nearest256, type Rgb } from "./palette.ts";

/** The character that splits a cell in half. */
const LOWER_HALF_BLOCK = "▄";

/** Home the cursor, so a frame overwrites the last rather than scrolling. */
const CURSOR_HOME = "\x1b[H";

/** Reset at the end of every row, so a colour cannot leak past it. */
const ROW_RESET = "\x1b[0m\r\n";

/** Below this alpha a pixel is drawn as black. */
const OPACITY_THRESHOLD = 128;

/** How many colours a terminal is asked for. */
export type ColorMode = "truecolor" | "256" | "16";

/** How a colour pair becomes an escape sequence. */
export type SgrEmitter = (fg: Rgb, bg: Rgb) => string;

/** Twenty-four bits per pixel, which every modern terminal understands. */
export function sgrTruecolor(fg: Rgb, bg: Rgb): string {
  return `\x1b[38;2;${fg[0]};${fg[1]};${fg[2]};48;2;${bg[0]};${bg[1]};${bg[2]}m`;
}

/** The xterm palette, for a terminal that has it but not truecolour. */
export function sgr256(fg: Rgb, bg: Rgb): string {
  return `\x1b[38;5;${nearest256(...fg)};48;5;${nearest256(...bg)}m`;
}

/**
 * The classic sixteen.
 *
 * Each colour is quantised separately and only the code that applies is taken
 * — the foreground code from the foreground's match, the background code from
 * the background's.
 */
export function sgr16(fg: Rgb, bg: Rgb): string {
  const [fgCode] = nearest16(...fg);
  const [, bgCode] = nearest16(...bg);
  return `\x1b[${fgCode};${bgCode}m`;
}

/** The emitter for each colour mode. */
export const SGR_EMITTERS: Readonly<Record<ColorMode, SgrEmitter>> = {
  truecolor: sgrTruecolor,
  "256": sgr256,
  "16": sgr16,
};

/** A pixel, with its opacity. */
export type Rgba = readonly [number, number, number, number];

/** How the renderer reads an image. */
export type PixelReader = (x: number, y: number) => Rgba;

/** A pixel drawn as black where it is too transparent to draw at all. */
function opaque(pixel: Rgba): Rgb {
  // There is no way to punch a hole in a terminal cell, so a transparent
  // pixel becomes black and the cell is still drawn.
  return pixel[3] < OPACITY_THRESHOLD ? [0, 0, 0] : [pixel[0], pixel[1], pixel[2]];
}

/**
 * Render one frame.
 *
 * A repeated colour pair emits no escape: the comparison is against the last
 * sequence written *on that row*, so a run of identical pixels costs one
 * escape and then nothing. That is the difference between a frame that fits
 * in a terminal's buffer and one that does not.
 */
export function renderFrame(pixels: PixelReader, width: number, height: number, emit: SgrEmitter): string {
  const parts: string[] = [CURSOR_HOME];

  for (let y = 0; y < height; y += 2) {
    const row: string[] = [];
    let previous = "";

    for (let x = 0; x < width; x += 1) {
      const top = opaque(pixels(x, y));
      // An odd number of pixel rows pairs the last with black, rather than
      // dropping it or reading past the end of the image.
      const bottom = y + 1 < height ? opaque(pixels(x, y + 1)) : ([0, 0, 0] as Rgb);

      const sgr = emit(bottom, top);
      if (sgr !== previous) {
        row.push(sgr);
        previous = sgr;
      }
      row.push(LOWER_HALF_BLOCK);
    }

    // Reset per row rather than per frame, so a colour cannot leak into
    // whatever a terminal draws next.
    row.push(ROW_RESET);
    parts.push(row.join(""));
  }
  return parts.join("");
}
