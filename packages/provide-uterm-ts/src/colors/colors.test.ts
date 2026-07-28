//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  applyColorMode,
  applyColorModeBytes,
  downgradeTo16,
  downgradeTo256,
  rewriteParams,
  rgbTo16Index,
  rgbTo256,
} from "./index.ts";

interface ColorsGolden {
  rgb: Array<{ r: number; g: number; b: number; to256: number; to16: number }>;
  sgr: Array<{ params: string; mode256: string; mode16: string }>;
  text: Array<{
    text: string;
    to256: string;
    to16: string;
    passthrough: string;
    bytes256: string;
    bytes16: string;
  }>;
}

const golden = loadGolden<ColorsGolden>("colors_golden.json");

/** Render bytes as lowercase hex, matching Python's `bytes.hex()`. */
function hex(data: Uint8Array): string {
  return Buffer.from(data).toString("hex");
}

/** Encode a string as latin-1 bytes, matching Python's `str.encode("latin-1")`. */
function latin1(text: string): Uint8Array {
  return new Uint8Array(Buffer.from(text, "latin1"));
}

describe("rgbTo256", () => {
  it.each([
    ["maps pure black to the cube floor", 0, 0, 0, 16],
    ["keeps greys under 8 off the ramp", 7, 7, 7, 16],
    ["starts the greyscale ramp at 8", 8, 8, 8, 232],
    ["ends the greyscale ramp at 248", 248, 248, 248, 255],
    ["pushes greys over 248 to the cube ceiling", 249, 249, 249, 231],
    ["maps pure white to the cube ceiling", 255, 255, 255, 231],
    ["maps pure red into the cube", 255, 0, 0, 196],
    ["maps pure green into the cube", 0, 255, 0, 46],
    ["maps pure blue into the cube", 0, 0, 255, 21],
    ["clamps components above 255", 300, 0, 0, 196],
    ["clamps components below 0", -1, 0, 0, 16],
    ["quantises a midrange non-grey", 128, 64, 200, 134],
  ])("%s", (_name, r, g, b, want) => {
    expect(rgbTo256(r as number, g as number, b as number)).toBe(want);
  });

  it("rounds half to even like CPython at the cube seams", () => {
    // 127 / 255 * 5 == 2.4901…  →  2, but 128 / 255 * 5 == 2.5098… → 3.
    // The exact .5 seam only appears via the clamped 255 endpoint, which the
    // golden corpus covers exhaustively; these two bracket the boundary.
    expect(rgbTo256(127, 0, 0)).toBe(16 + 36 * 2);
    expect(rgbTo256(128, 0, 0)).toBe(16 + 36 * 3);
  });
});

describe("rgbTo16Index", () => {
  it.each([
    ["maps black to index 0", 0, 0, 0, 0],
    ["maps bright white to index 15", 255, 255, 255, 15],
    ["maps the palette red to index 4", 205, 0, 0, 4],
    ["maps the palette bright red to index 12", 255, 92, 92, 12],
    ["clamps components above 255", 999, 999, 999, 15],
    ["clamps components below 0", -5, -5, -5, 0],
  ])("%s", (_name, r, g, b, want) => {
    expect(rgbTo16Index(r as number, g as number, b as number)).toBe(want);
  });
});

describe("rewriteParams", () => {
  it("emits a bare SGR reset for an empty parameter list", () => {
    expect(rewriteParams("", "256")).toBe("\x1b[m");
    expect(rewriteParams("", "16")).toBe("\x1b[m");
  });

  it("rewrites a foreground truecolor run to the 256 cube", () => {
    expect(rewriteParams("38;2;255;0;0", "256")).toBe("\x1b[38;5;196m");
  });

  it("rewrites a background truecolor run to the 16-color palette", () => {
    expect(rewriteParams("48;2;0;0;255", "16")).toBe("\x1b[44m");
  });

  it("preserves surrounding parameters in place and order", () => {
    // Expectation taken from CPython: rewrite_params("1;38;2;12;34;56;4", "256").
    expect(rewriteParams("1;38;2;12;34;56;4", "256")).toBe("\x1b[1;38;5;23;4m");
  });

  it("rewrites consecutive truecolor runs independently", () => {
    expect(rewriteParams("38;2;10;20;30;48;2;40;50;60", "16")).toBe("\x1b[30;40m");
  });

  it("leaves a truncated truecolor run untouched", () => {
    expect(rewriteParams("38;2;1;2", "256")).toBe("\x1b[38;2;1;2m");
  });

  it("leaves an indexed 256-color run untouched", () => {
    expect(rewriteParams("38;5;196", "16")).toBe("\x1b[38;5;196m");
  });

  it("leaves a non-digit component untouched when called outside the SGR scanner", () => {
    // The SGR pattern only ever yields [0-9;], so this branch is reachable
    // only through a direct call. CPython raises on `int("2a")` here; passing
    // the run through unchanged is the strictly safer divergence.
    expect(rewriteParams("38;2;1;2a;3", "256")).toBe("\x1b[38;2;1;2a;3m");
    expect(rewriteParams("48;2;1;2;3z", "16")).toBe("\x1b[48;2;1;2;3zm");
  });
});

describe("downgradeTo256 / downgradeTo16", () => {
  it("leaves text with no escape sequences unchanged", () => {
    expect(downgradeTo256("plain text")).toBe("plain text");
    expect(downgradeTo16("plain text")).toBe("plain text");
  });

  it("is idempotent on already-downgraded text", () => {
    const once = downgradeTo256("\x1b[38;2;255;0;0mred\x1b[0m");
    expect(downgradeTo256(once)).toBe(once);
  });

  it("rewrites every occurrence, not just the first", () => {
    // Expectation taken from CPython: (255,0,0) is nearest palette index 4
    // (205,0,0) — not the bright variant 12 (255,92,92).
    expect(downgradeTo16("\x1b[38;2;255;0;0ma\x1b[38;2;0;0;255mb")).toBe("\x1b[31ma\x1b[34mb");
  });

  it("does not carry scanner state between calls", () => {
    const input = "\x1b[38;2;255;0;0mred";
    expect(downgradeTo256(input)).toBe(downgradeTo256(input));
  });
});

describe("applyColorMode", () => {
  it("returns the input unchanged in passthrough mode", () => {
    const text = "\x1b[38;2;255;0;0mred";
    expect(applyColorMode(text, "passthrough")).toBe(text);
  });

  it("dispatches to the 256 and 16 downgraders", () => {
    const text = "\x1b[38;2;255;0;0mred";
    expect(applyColorMode(text, "256")).toBe(downgradeTo256(text));
    expect(applyColorMode(text, "16")).toBe(downgradeTo16(text));
  });
});

describe("applyColorModeBytes", () => {
  it("returns the input unchanged in passthrough mode", () => {
    const data = latin1("\x1b[38;2;255;0;0mred");
    expect(applyColorModeBytes(data, "passthrough")).toBe(data);
  });

  it("round-trips bytes above 0x7f through the latin-1 mapping", () => {
    expect(hex(applyColorModeBytes(latin1("\xff\x1b[38;2;255;0;0m\xfe"), "256"))).toBe(
      hex(latin1("\xff\x1b[38;5;196m\xfe")),
    );
  });
});

describe("differential parity with CPython", () => {
  it("matches every rgb golden record", () => {
    for (const record of golden.rgb) {
      expect({
        to256: rgbTo256(record.r, record.g, record.b),
        to16: rgbTo16Index(record.r, record.g, record.b),
      }).toStrictEqual({ to256: record.to256, to16: record.to16 });
    }
    expect(golden.rgb.length).toBeGreaterThan(300);
  });

  it("matches every SGR parameter golden record", () => {
    for (const record of golden.sgr) {
      expect(rewriteParams(record.params, "256")).toBe(record.mode256);
      expect(rewriteParams(record.params, "16")).toBe(record.mode16);
    }
    expect(golden.sgr.length).toBeGreaterThan(10);
  });

  it("matches every text golden record on the string path", () => {
    for (const record of golden.text) {
      expect(downgradeTo256(record.text)).toBe(record.to256);
      expect(downgradeTo16(record.text)).toBe(record.to16);
      expect(applyColorMode(record.text, "256")).toBe(record.to256);
      expect(applyColorMode(record.text, "16")).toBe(record.to16);
      expect(applyColorMode(record.text, "passthrough")).toBe(record.passthrough);
    }
    expect(golden.text.length).toBeGreaterThan(10);
  });

  it("matches every text golden record on the bytes path", () => {
    for (const record of golden.text) {
      expect(hex(applyColorModeBytes(latin1(record.text), "256"))).toBe(record.bytes256);
      expect(hex(applyColorModeBytes(latin1(record.text), "16"))).toBe(record.bytes16);
    }
  });
});
