//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  BOLD,
  CLEAR_SCREEN,
  color256ToRgb,
  DEFAULT_PALETTE,
  DEFAULT_RGB,
  emitColor,
  handleBraceTokens,
  handleExtendedTokens,
  handlePipeCodes,
  handleTildeCodes,
  normalizeColors,
  RESET,
  registerColorDialect,
  registeredDialects,
  unregisterColorDialect,
  upgradeTo256,
  upgradeToTruecolor,
} from "./index.ts";

interface AnsiGolden {
  constants: {
    DEFAULT_PALETTE: number[];
    DEFAULT_RGB: number[][];
    CLEAR_SCREEN: string;
    BOLD: string;
    RESET: string;
    registered_dialects: string[];
  };
  color256_to_rgb: Array<{ index: number; rgb: number[] }>;
  extended_tokens: Array<{ text: string; out: string }>;
  tilde_codes: Array<{ text: string; out: string }>;
  brace_tokens: Array<{ text: string; out: string }>;
  pipe_codes: Array<{ text: string; out: string }>;
  normalize: Array<{ text: string; out: string }>;
  upgrade_256: Array<{ text: string; out: string }>;
  upgrade_truecolor: Array<{ text: string; out: string }>;
  upgrade_custom_palette: Array<{ text: string; to256: string; truecolor: string }>;
  dialect_divergences: Array<{ text: string; extended: string; pipe: string }>;
}

const golden = loadGolden<AnsiGolden>("ansi_golden.json");

/** The non-default palette the corpus used, proving the argument is honoured. */
const CUSTOM_PALETTE = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16];

/** Compare a whole section against its recorded output. */
function expectSection(section: Array<{ text: string; out: string }>, transform: (text: string) => string): void {
  for (const record of section) {
    expect({ text: record.text, out: transform(record.text) }).toStrictEqual(record);
  }
}

describe("palette constants", () => {
  it("matches the reference palette tables", () => {
    expect(DEFAULT_PALETTE).toStrictEqual(golden.constants.DEFAULT_PALETTE);
    expect(DEFAULT_RGB.map((rgb) => [...rgb])).toStrictEqual(golden.constants.DEFAULT_RGB);
  });

  it("exposes the common escape sequences", () => {
    expect({ CLEAR_SCREEN, BOLD, RESET }).toStrictEqual({
      CLEAR_SCREEN: golden.constants.CLEAR_SCREEN,
      BOLD: golden.constants.BOLD,
      RESET: golden.constants.RESET,
    });
  });
});

describe("color256ToRgb", () => {
  it("returns the base palette entry below index 16", () => {
    expect(color256ToRgb(0)).toStrictEqual([0, 0, 0]);
    expect(color256ToRgb(15)).toStrictEqual([255, 255, 255]);
  });

  it("decodes the 6x6x6 cube", () => {
    expect(color256ToRgb(16)).toStrictEqual([0, 0, 0]);
    expect(color256ToRgb(231)).toStrictEqual([255, 255, 255]);
    expect(color256ToRgb(196)).toStrictEqual([255, 0, 0]);
  });

  it("decodes the greyscale ramp", () => {
    expect(color256ToRgb(232)).toStrictEqual([8, 8, 8]);
    expect(color256ToRgb(255)).toStrictEqual([238, 238, 238]);
  });

  it("matches every recorded index", () => {
    for (const record of golden.color256_to_rgb) {
      expect(color256ToRgb(record.index)).toStrictEqual(record.rgb);
    }
    expect(golden.color256_to_rgb).toHaveLength(256);
  });
});

describe("handleExtendedTokens", () => {
  it("converts a foreground token", () => {
    expect(handleExtendedTokens("{F196}")).toBe("\x1b[38;5;196m");
  });

  it("converts a background token", () => {
    expect(handleExtendedTokens("{B21}")).toBe("\x1b[48;5;21m");
  });

  it("wraps a palette token index into the base sixteen", () => {
    expect(handleExtendedTokens("{P16}")).toBe(handleExtendedTokens("{P0}"));
    expect(handleExtendedTokens("{P8}")).toBe("\x1b[90m");
  });

  it("computes an escape for a value past the lookup table", () => {
    expect(handleExtendedTokens("{F999}")).toBe("\x1b[38;5;999m");
  });

  it("returns the input unchanged when there are no tokens", () => {
    expect(handleExtendedTokens("plain")).toBe("plain");
  });

  it("leaves a malformed token verbatim", () => {
    expect(handleExtendedTokens("{F}")).toBe("{F}");
    expect(handleExtendedTokens("{F1234}")).toBe("{F1234}");
    expect(handleExtendedTokens("{Z1}")).toBe("{Z1}");
  });
});

describe("handleTildeCodes", () => {
  it("converts a mapped numeric code", () => {
    expect(handleTildeCodes("~1")).toBe("\x1b[0;1;32m");
  });

  it("converts a mapped letter code in both cases", () => {
    expect(handleTildeCodes("~r")).toBe(handleTildeCodes("~R"));
  });

  it("emits a plain reset for the reset code", () => {
    expect(handleTildeCodes("~0")).toBe("\x1b[0m");
  });

  it("re-emits an unmapped code with its tilde", () => {
    expect(handleTildeCodes("~z")).toBe("~z");
    expect(handleTildeCodes("~8")).toBe("~8");
  });

  it("leaves a trailing lone tilde alone", () => {
    expect(handleTildeCodes("text~")).toBe("text~");
  });

  it("does not match a tilde before a newline, because the pattern is not DOTALL", () => {
    expect(handleTildeCodes("~\n")).toBe("~\n");
  });

  it("returns the input unchanged when there are no tildes", () => {
    expect(handleTildeCodes("plain")).toBe("plain");
  });
});

describe("emitColor", () => {
  it("emits a bright sequence for a positive polarity", () => {
    expect(emitColor("+", "r")).toBe("\x1b[0;1;31m");
  });

  it("emits a dim sequence for a negative polarity", () => {
    expect(emitColor("-", "r")).toBe("\x1b[0;31m");
  });

  it("emits a plain reset for the reset colour, whatever the polarity", () => {
    expect(emitColor("+", "x")).toBe("\x1b[0m");
    expect(emitColor("-", "x")).toBe("\x1b[0m");
  });

  it("emits nothing for a colour it does not know", () => {
    // Unreachable through the tilde table, which maps every code it defines;
    // the reference marks the same branch as uncovered.
    expect(emitColor("+", "z")).toBe("");
  });
});

describe("handleBraceTokens", () => {
  it("converts a bright and a dim tag", () => {
    expect(handleBraceTokens("{+c}")).toBe("\x1b[1;36m");
    expect(handleBraceTokens("{-c}")).toBe("\x1b[0;36m");
  });

  it("converts the reset tags", () => {
    expect(handleBraceTokens("{-x}")).toBe("\x1b[0m");
    expect(handleBraceTokens("{NK}")).toBe("\x1b[0m");
  });

  it("matches the four-character TWGS token before the shorter ones", () => {
    expect(handleBraceTokens("{+Bw}")).toBe("\x1b[1;37m");
  });

  it("leaves an unmapped tag verbatim", () => {
    expect(handleBraceTokens("{+z}")).toBe("{+z}");
    expect(handleBraceTokens("{nk}")).toBe("{nk}");
  });

  it("returns the input unchanged when there are no tokens", () => {
    expect(handleBraceTokens("plain")).toBe("plain");
  });
});

describe("handlePipeCodes", () => {
  it("converts a dim foreground code", () => {
    expect(handlePipeCodes("|00")).toBe("\x1b[30m");
    expect(handlePipeCodes("|07")).toBe("\x1b[37m");
  });

  it("converts a bright foreground code by adding sixty", () => {
    expect(handlePipeCodes("|08")).toBe("\x1b[90m");
    expect(handlePipeCodes("|15")).toBe("\x1b[97m");
  });

  it("converts a background code", () => {
    expect(handlePipeCodes("|16")).toBe("\x1b[40m");
    expect(handlePipeCodes("|23")).toBe("\x1b[47m");
  });

  it("re-emits a code past the table with its pipe", () => {
    expect(handlePipeCodes("|24")).toBe("|24");
    expect(handlePipeCodes("|99")).toBe("|99");
  });

  it("ignores a code that is not exactly two digits", () => {
    expect(handlePipeCodes("|0")).toBe("|0");
    expect(handlePipeCodes("|ab")).toBe("|ab");
  });

  it("returns the input unchanged when there are no pipes", () => {
    expect(handlePipeCodes("plain")).toBe("plain");
  });
});

describe("dialect registry", () => {
  afterEach(() => {
    for (const name of registeredDialects()) {
      if (name === "test_dialect") {
        unregisterColorDialect(name);
      }
    }
  });

  it("registers the four built-in dialects in call order", () => {
    expect(registeredDialects()).toStrictEqual(golden.constants.registered_dialects);
  });

  it("runs a newly registered handler as part of normalizeColors", () => {
    registerColorDialect("test_dialect", (text) => text.replace("@@", "!"));
    expect(normalizeColors("a@@b")).toBe("a!b");
  });

  it("refuses to register the same name twice", () => {
    registerColorDialect("test_dialect", (text) => text);
    expect(() => registerColorDialect("test_dialect", (text) => text)).toThrow(
      'color dialect "test_dialect" is already registered',
    );
  });

  it("refuses to unregister a name that is not registered", () => {
    expect(() => unregisterColorDialect("nope")).toThrow('color dialect "nope" is not registered');
  });

  it("removes a handler on unregister", () => {
    registerColorDialect("test_dialect", (text) => `${text}!`);
    unregisterColorDialect("test_dialect");
    expect(registeredDialects()).toStrictEqual(golden.constants.registered_dialects);
    expect(normalizeColors("a")).toBe("a");
  });

  it("returns a copy of the dialect list", () => {
    const names = registeredDialects();
    names.push("injected");
    expect(registeredDialects()).toStrictEqual(golden.constants.registered_dialects);
  });
});

describe("upgradeTo256", () => {
  it("replaces a dim foreground with its palette entry", () => {
    expect(upgradeTo256("\x1b[31mX")).toBe(`\x1b[38;5;${DEFAULT_PALETTE[1]}mX`);
  });

  it("promotes a bold foreground to its bright palette entry", () => {
    expect(upgradeTo256("\x1b[1;31mX")).toBe(`\x1b[1;38;5;${DEFAULT_PALETTE[9]}mX`);
  });

  it("does not promote a background when bold is set", () => {
    expect(upgradeTo256("\x1b[1;41mX")).toBe(`\x1b[1;48;5;${DEFAULT_PALETTE[1]}mX`);
  });

  it("leaves an already-upgraded sequence alone", () => {
    expect(upgradeTo256("\x1b[38;5;196mX")).toBe("\x1b[38;5;196mX");
    expect(upgradeTo256("\x1b[48;5;21mX")).toBe("\x1b[48;5;21mX");
  });

  it("leaves an empty parameter list alone", () => {
    expect(upgradeTo256("\x1b[mX")).toBe("\x1b[mX");
  });

  it("keeps non-colour parameters in place", () => {
    expect(upgradeTo256("\x1b[4;31mX")).toBe(`\x1b[4;38;5;${DEFAULT_PALETTE[1]}mX`);
  });

  it("honours a custom palette", () => {
    expect(upgradeTo256("\x1b[31mX", CUSTOM_PALETTE)).toBe("\x1b[38;5;2mX");
  });
});

describe("upgradeToTruecolor", () => {
  it("replaces a dim foreground with its RGB triple", () => {
    const [r, g, b] = color256ToRgb(DEFAULT_PALETTE[1] as number);
    expect(upgradeToTruecolor("\x1b[31mX")).toBe(`\x1b[38;2;${r};${g};${b}mX`);
  });

  it("emits a full escape for a palette token, not a brace token", () => {
    expect(upgradeToTruecolor("{P1}")).toMatch(/^\x1b\[38;2;\d+;\d+;\d+m$/);
  });

  it("honours a custom palette", () => {
    const [r, g, b] = color256ToRgb(2);
    expect(upgradeToTruecolor("\x1b[31mX", CUSTOM_PALETTE)).toBe(`\x1b[38;2;${r};${g};${b}mX`);
  });
});

describe("differential parity with CPython", () => {
  it("matches every extended-token record", () => {
    expectSection(golden.extended_tokens, handleExtendedTokens);
    expect(golden.extended_tokens.length).toBeGreaterThan(70);
  });

  it("matches every tilde-code record", () => {
    expectSection(golden.tilde_codes, handleTildeCodes);
    expect(golden.tilde_codes.length).toBeGreaterThan(35);
  });

  it("matches every brace-token record", () => {
    expectSection(golden.brace_tokens, handleBraceTokens);
    expect(golden.brace_tokens.length).toBeGreaterThan(30);
  });

  it("matches every pipe-code record", () => {
    expectSection(golden.pipe_codes, handlePipeCodes);
    expect(golden.pipe_codes.length).toBeGreaterThan(35);
  });

  it("matches every normalizeColors record", () => {
    expectSection(golden.normalize, normalizeColors);
    expect(golden.normalize.length).toBeGreaterThan(180);
  });

  it("matches every 256-colour upgrade record", () => {
    expectSection(golden.upgrade_256, upgradeTo256);
    expect(golden.upgrade_256.length).toBeGreaterThan(60);
  });

  it("matches every truecolor upgrade record", () => {
    expectSection(golden.upgrade_truecolor, upgradeToTruecolor);
    expect(golden.upgrade_truecolor.length).toBeGreaterThan(60);
  });

  it("matches every custom-palette upgrade record", () => {
    for (const record of golden.upgrade_custom_palette) {
      expect({
        text: record.text,
        to256: upgradeTo256(record.text, CUSTOM_PALETTE),
        truecolor: upgradeToTruecolor(record.text, CUSTOM_PALETTE),
      }).toStrictEqual(record);
    }
  });

  it("documents where token digit classes diverge from CPython", () => {
    // CPython reads \d as Unicode-aware for str subjects, so it accepts
    // Arabic-Indic digits inside a token and int() parses them. ECMAScript
    // and Go's RE2 read \d as ASCII-only, so the token is left verbatim.
    const divergences = golden.dialect_divergences.map((record) => ({
      text: record.text,
      cpython: record.extended,
      host: handleExtendedTokens(record.text),
    }));
    expect(divergences).toStrictEqual([
      { text: "{F١٢٣}", cpython: "\x1b[38;5;123m", host: "{F١٢٣}" },
      { text: "|٠٧", cpython: "|٠٧", host: "|٠٧" },
      { text: "{P٥}", cpython: "\x1b[35m", host: "{P٥}" },
    ]);
  });

  it("agrees with CPython on pipe codes even for Unicode digits", () => {
    // The pipe lookup is keyed by the matched text, so a Unicode-digit code
    // misses the table on both sides and is re-emitted verbatim.
    for (const record of golden.dialect_divergences) {
      expect(handlePipeCodes(record.text)).toBe(record.pipe);
    }
  });
});
