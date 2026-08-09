//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden, must } from "../testing/golden.ts";
import { convertType, type ExtractConfig, extractKV } from "./index.ts";

interface ConvertCase {
  name: string;
  value: string;
  type: string;
  ok: boolean;
  value_out?: unknown;
  is_nan?: boolean;
  is_infinite?: boolean;
  is_bool?: boolean;
  error?: string;
}

interface ExtractorGolden {
  screen: string;
  single: Array<{ name: string; screen: string; config: ExtractConfig; result: Record<string, unknown> | null }>;
  convert: Array<ConvertCase & { value: string; ok: boolean }>;
  extract: Array<{
    name: string;
    screen: string;
    config: ExtractConfig | ExtractConfig[] | null;
    result: Record<string, unknown> | null;
  }>;
  unvalidated: Array<{ name: string; result: Record<string, unknown> | null }>;
  convenience_matches_extract: boolean;
}

const golden = loadGolden<ExtractorGolden>("extractor_golden.json");

/**
 * Rebuild what JSON could not carry.
 *
 * A float field can extract to an infinity or a NaN, because Python's
 * `float()` reads those words. Neither survives JSON, so the corpus records
 * a marker.
 */
function rebuilt(result: Record<string, unknown> | null): Record<string, unknown> | null {
  if (result === null) {
    return null;
  }
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(result)) {
    const marker = (value as { __float__?: string } | null)?.__float__;
    out[key] =
      marker === "nan"
        ? Number.NaN
        : marker === "inf"
          ? Number.POSITIVE_INFINITY
          : marker === "-inf"
            ? Number.NEGATIVE_INFINITY
            : value;
  }
  return out;
}

/** The recorded conversion for a named case. */
function convertCase(name: string) {
  return golden.convert.find((entry) => entry.name === name);
}

/** The recorded extraction for a named case. */
function extractCase(name: string) {
  return golden.extract.find((entry) => entry.name === name);
}

describe("pulling one field out of a screen", () => {
  it.each(golden.single)("$name", (record) => {
    expect(extractKV(record.screen, record.config, false) ?? null).toStrictEqual(rebuilt(record.result));
  });

  it("takes the last match, not the first", () => {
    // A screen buffer holds scroll history, so the same label appears many
    // times and only the bottom one is current. Reading the first would spend
    // a credit balance from several screens ago.
    expect(extractCase("several fields")?.result?.sector).toBe(42);
    expect(golden.screen).toContain("Sector  1");
    expect(golden.single.find((entry) => entry.name === "the last of several")?.result?.sector).toBe(42);
  });

  it("prefers a capture group over the whole match", () => {
    // So a pattern written with parentheses yields the number rather than the
    // label and the number together.
    expect(golden.single.find((entry) => entry.name === "an int")?.result?.sector).toBe(42);
  });

  it("uses the whole match when there is no group", () => {
    expect(golden.single.find((entry) => entry.name === "no capture group takes the whole match")?.result?.sector).toBe(
      42,
    );
  });

  it("takes the first group when there are several", () => {
    expect(golden.single.find((entry) => entry.name === "two groups take the first")?.result?.pair).toBe("1");
  });

  it("drops a field it could not convert rather than guessing", () => {
    // A zero where the screen said "abc" is worse than nothing: a caller
    // cannot tell it apart from a real zero.
    for (const name of ["an int that is not one", "a float-shaped string as an int", "a bool that is not one"]) {
      expect(golden.single.find((entry) => entry.name === name)?.result).toBeNull();
    }
  });

  it("treats a field name of zero as no field name", () => {
    // Falsy in the reference, so the whole config is skipped rather than a
    // field called "0" appearing in the result.
    expect(extractCase("a field name that is zero")?.result).toBeNull();
  });

  it("does not turn an absent pattern into the word undefined", () => {
    // A config with no regex is skipped. Stringifying the missing value would
    // make it a pattern that matches screens saying "undefined".
    expect(extractCase("a screen that says undefined")?.result).toBeNull();
  });

  it("needs both a field name and a pattern", () => {
    for (const name of ["no field name", "no regex", "an empty field name", "an empty regex"]) {
      expect(golden.single.find((entry) => entry.name === name)?.result).toBeNull();
    }
  });

  it("matches case-insensitively and per line", () => {
    // Both are on by default. A screen's case drifts between versions of a
    // program, and a value can be anywhere on it.
    expect(golden.single.find((entry) => entry.name === "case-insensitive by default")?.result?.sector).toBe(42);
    expect(golden.single.find((entry) => entry.name === "multiline by default")?.result?.sector).toBe(42);
  });

  it("strips the whitespace around a value", () => {
    expect(golden.single.find((entry) => entry.name === "whitespace is stripped")?.result?.name).toBe("Alice");
  });

  it("falls back to text for a type it does not know", () => {
    // Better a string than nothing: the rule author still gets their value.
    expect(golden.single.find((entry) => entry.name === "an unknown type falls back to text")?.result?.sector).toBe(
      "42",
    );
  });
});

describe("converting a matched string", () => {
  it.each(golden.convert)("$name as $type", (record) => {
    if (!record.ok) {
      expect(convertType(record.value, record.type)).toBeUndefined();
      return;
    }
    const converted = convertType(record.value, record.type);
    if (record.is_nan === true) {
      expect(converted).toBeNaN();
    } else if (record.is_infinite !== undefined) {
      expect(converted).toBe(record.is_infinite ? Number.POSITIVE_INFINITY : Number.NEGATIVE_INFINITY);
    } else {
      expect(converted).toStrictEqual(record.value_out);
    }
  });

  it("strips thousands separators", () => {
    // Screens write numbers for people to read.
    expect(convertCase("an integer with separators")?.value_out).toBe(1234567);
    expect(convertCase("a float with separators")?.value_out).toBe(1234.5);
  });

  it("refuses a float-shaped string as an integer", () => {
    // Truncating it would turn 1.9 sectors into sector 1 without saying so.
    expect(convertCase("a float as an int")?.ok).toBe(false);
    expect(convertType("1.9", "int")).toBeUndefined();
  });

  it("accepts the underscores Python's own literals allow", () => {
    expect(convertCase("an integer with underscores")?.value_out).toBe(1000);
  });

  it("refuses an empty string as a number", () => {
    // Number("") is zero in this language, which is the single most likely
    // way an unread screen becomes a plausible-looking value.
    expect(convertType("", "int")).toBeUndefined();
    expect(convertType("", "float")).toBeUndefined();
    expect(convertType("   ", "float")).toBeUndefined();
  });

  it("reads the float words Python reads", () => {
    expect(convertType("inf", "float")).toBe(Number.POSITIVE_INFINITY);
    expect(convertType("-inf", "float")).toBe(Number.NEGATIVE_INFINITY);
    expect(convertType("nan", "float")).toBeNaN();
    expect(convertType("infinity", "float")).toBe(Number.POSITIVE_INFINITY);
  });

  it("does not read the ones it does not", () => {
    // A hexadecimal literal is a number to this language and not to Python,
    // so a screen showing "0x10" must not become 16.
    expect(convertType("0x10", "int")).toBeUndefined();
    expect(convertType("0x10", "float")).toBeUndefined();
    expect(convertType("1e5", "int")).toBeUndefined();
  });

  it("takes the truthy words and the falsy ones", () => {
    for (const name of ["true", "yes", "y", "one", "on", "upper case true", "mixed case yes"]) {
      expect(convertCase(name)?.value_out).toBe(true);
    }
    for (const name of ["false", "no", "n", "zero", "off"]) {
      expect(convertCase(name)?.value_out).toBe(false);
    }
  });

  it("refuses a word that is neither", () => {
    // "maybe" is not a decision, and guessing one is how an agent confirms
    // something it was not asked to confirm.
    expect(convertCase("something else as a bool")?.ok).toBe(false);
    expect(convertType("maybe", "bool")).toBeUndefined();
  });

  it("keeps a bool a bool", () => {
    // Not one and zero: a caller checking `=== true` would otherwise never
    // match.
    expect(convertType("yes", "bool")).toBe(true);
    expect(convertType("no", "bool")).toBe(false);
  });
});

describe("extracting a whole configuration", () => {
  it.each(golden.extract)("$name", (record) => {
    expect(extractKV(record.screen, record.config) ?? null).toStrictEqual(rebuilt(record.result));
  });

  it.each(golden.unvalidated)("$name, without validation", (record) => {
    const source = extractCase(record.name);
    expect(extractKV(source?.screen as string, source?.config as ExtractConfig[], false) ?? null).toStrictEqual(
      rebuilt(record.result),
    );
  });

  it("accepts a single configuration as well as a list", () => {
    expect(extractCase("a single config rather than a list")?.result?.sector).toBe(42);
  });

  it("says nothing when there is nothing to do", () => {
    for (const name of ["nothing configured", "an empty list", "an empty dict"]) {
      expect(extractCase(name)?.result).toBeNull();
    }
  });

  it("refuses a configuration that is neither a list nor a field", () => {
    for (const name of ["a config that is neither", "a dict with no field key"]) {
      expect(extractCase(name)?.result).toBeNull();
    }
  });

  it("says nothing when nothing matched", () => {
    // An empty result and no result are different: the caller learns the
    // screen was not the one they expected.
    expect(extractCase("nothing matched")?.result).toBeNull();
  });

  it("keeps the fields it found when one is missing", () => {
    // Partial data beats none — the caller can still see what was on screen.
    const result = extractCase("one field matched and one not")?.result;
    expect(result?.sector).toBe(42);
    expect(Object.hasOwn(result as object, "credits")).toBe(false);
  });

  it("reports validation rather than enforcing it", () => {
    // The values come back alongside the verdict, so a caller can act on one
    // it knows is out of range — or refuse to. Dropping it would leave them
    // unable to tell "absent" from "implausible".
    const result = extractCase("a value below its minimum")?.result;
    expect(result?.sector).toBe(3);
    expect((must(result, "the below-minimum extraction")._validation as { valid: boolean }).valid).toBe(false);
  });

  it("leaves the report out when it was not asked for", () => {
    const index = golden.extract.findIndex((entry) => entry.name === "several fields");
    expect(Object.hasOwn(golden.unvalidated[index]?.result as object, "_validation")).toBe(false);
    expect(Object.hasOwn(golden.extract[index]?.result as object, "_validation")).toBe(true);
  });
});

describe("what validation reports", () => {
  /** The verdict recorded for a named case. */
  function verdict(name: string) {
    return extractCase(name)?.result?._validation as { valid: boolean; errors: string[] } | undefined;
  }

  it("passes a value inside its range", () => {
    expect(verdict("a value inside its range")?.valid).toBe(true);
    expect(verdict("a value inside its range")?.errors).toStrictEqual([]);
  });

  it("treats the bounds as inclusive", () => {
    // A value exactly on its limit is allowed; excluding it would reject the
    // first and last legitimate values.
    expect(verdict("a value exactly on its bounds")?.valid).toBe(true);
  });

  it("names the field, the value and the bound it broke", () => {
    // The message is what an operator reads; a bare "invalid" would send them
    // back to the rules file to guess which one.
    expect(verdict("a value below its minimum")?.errors[0]).toContain("sector");
    expect(verdict("a value below its minimum")?.errors[0]).toContain("3");
    expect(verdict("a value above its maximum")?.errors[0]).toContain("100");
  });

  it("reports a required field that was not found", () => {
    const result = verdict("a required field that is missing");
    expect(result?.valid).toBe(false);
    expect(result?.errors[0]).toContain("required");
  });

  it("says nothing about an optional field that was not found", () => {
    expect(verdict("one field matched and one not")?.valid).toBe(true);
  });

  it("anchors a pattern at the start and not at the end", () => {
    // The reference matches rather than fullmatches, so a rule saying "^A"
    // means "begins with A" and not "is exactly A".
    expect(verdict("a string against a pattern")?.valid).toBe(true);
    expect(verdict("a string failing its pattern")?.valid).toBe(false);
    expect(verdict("a pattern anchored only at the start")?.valid).toBe(true);
  });

  it("prints an allowed set the way the reference does", () => {
    // The message is compared against the reference's own output, and Python
    // renders a list with single quotes around its strings, bare numbers, and
    // a space after each comma. JSON does none of that.
    expect(verdict("allowed values that are numbers")?.errors[0]).toBe(
      "sector: value '42' not in allowed values [1, 2, '3']",
    );
  });

  it("checks a value against its allowed set", () => {
    expect(verdict("a string in its allowed values")?.valid).toBe(true);
    expect(verdict("a string outside its allowed values")?.valid).toBe(false);
  });

  it("validates a float the same way as an int", () => {
    expect(verdict("a float validated as one")?.valid).toBe(true);
  });

  it("accepts a float that happens to be a whole number", () => {
    // The reference converted it with float(), so 2 is a float there however
    // it prints. Demanding a fractional part would reject every round value a
    // screen shows.
    expect(verdict("a float that is a whole number")?.valid).toBe(true);
    expect(extractCase("a float that is a whole number")?.result?.ratio).toBe(2);
  });

  it("reports a value checked against a type it is not", () => {
    // Two configs may name one field: the last to extract wins the value, and
    // every config still validates against it. So a string can be checked as
    // an int, and the message has to say which it found.
    expect(verdict("a value checked against a type it is not")?.errors[0]).toBe("sector: expected int, got str");
    expect(verdict("a fraction checked as a whole number")?.errors[0]).toBe("ratio: expected int, got float");
  });

  it("names the type it found in a mismatch", () => {
    expect(verdict("a whole number checked as text")?.errors[0]).toBe("sector: expected string, got int");
    expect(verdict("a boolean checked as text")?.errors[0]).toBe("docked: expected string, got bool");
    expect(verdict("a boolean checked as a fraction")?.errors[0]).toBe("docked: expected float, got bool");
  });

  it("counts a boolean as a whole number but not as a fraction", () => {
    // A bool is an int in the reference's type system and is not a float.
    // Two configs may name one field, so a value converted as a bool can be
    // validated as a number — refusing it would report an error the
    // reference does not.
    expect(verdict("a boolean checked as a whole number")?.valid).toBe(true);
    expect(verdict("a boolean checked as a fraction")?.valid).toBe(false);
  });

  it("reads a required flag by truthiness", () => {
    // A rules file writing 1 means required. Reading it as optional would let
    // a missing field pass in silence.
    expect(verdict("a required flag that is not a boolean")?.valid).toBe(false);
    expect(verdict("a required flag that is not a boolean")?.errors[0]).toContain("required");
  });

  it("anchors a pattern with no anchor of its own", () => {
    // The reference matches from the start, so a rule saying "lice" does not
    // match "Alice" — it is not a search.
    expect(verdict("a pattern with no anchor")?.valid).toBe(false);
  });

  it("needs no rules to pass", () => {
    expect(verdict("a bool needs no validation rules")?.valid).toBe(true);
  });

  it("reports every problem, not just the first", () => {
    // An operator fixing a rules file wants the whole list rather than one
    // error per run.
    expect(verdict("several problems at once")?.errors).toHaveLength(3);
  });

  it("skips a configuration whose field is not a string", () => {
    // The extraction pass does not check, so the value is present; the
    // validation pass does, so it is never checked. It is the one way a field
    // reaches a caller unvalidated.
    const result = extractCase("a config whose field is not a string")?.result;
    expect(result?.["7"]).toBe("42");
    expect((must(result, "the non-string-field extraction")._validation as { valid: boolean }).valid).toBe(true);
  });
});
