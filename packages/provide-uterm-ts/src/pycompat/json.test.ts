//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { pyJsonDumps } from "./index.ts";

interface PyJsonGolden {
  portable: Array<{ value: unknown; canonical: string; unsorted: string; unicode: string }>;
  float_divergences: Array<{ repr: string; canonical: string }>;
}

const golden = loadGolden<PyJsonGolden>("pyjson_golden.json");

describe("pyJsonDumps separators", () => {
  it("emits no whitespace between tokens", () => {
    expect(pyJsonDumps({ a: 1, b: [2, 3] })).toBe('{"a":1,"b":[2,3]}');
  });

  it("renders empty containers", () => {
    expect(pyJsonDumps({})).toBe("{}");
    expect(pyJsonDumps([])).toBe("[]");
  });
});

describe("pyJsonDumps key ordering", () => {
  it("sorts keys by default", () => {
    expect(pyJsonDumps({ b: 1, a: 2 })).toBe('{"a":2,"b":1}');
  });

  it("sorts by raw code unit, so uppercase precedes lowercase", () => {
    expect(pyJsonDumps({ B: 1, a: 2, A: 3, b: 4 })).toBe('{"A":3,"B":1,"a":2,"b":4}');
  });

  it("sorts numeric-looking keys as strings", () => {
    expect(pyJsonDumps({ "10": 1, "9": 2, "1": 3 })).toBe('{"1":3,"10":1,"9":2}');
  });

  it("preserves insertion order when sorting is turned off", () => {
    expect(pyJsonDumps({ b: 1, a: 2 }, { sortKeys: false })).toBe('{"b":1,"a":2}');
  });

  it("sorts nested objects too", () => {
    expect(pyJsonDumps({ outer: { b: 1, a: 2 } })).toBe('{"outer":{"a":2,"b":1}}');
  });
});

describe("pyJsonDumps string escaping", () => {
  it("escapes the two mandatory characters", () => {
    expect(pyJsonDumps('"')).toBe('"\\""');
    expect(pyJsonDumps("\\")).toBe('"\\\\"');
  });

  it("uses the short escapes for the named control characters", () => {
    expect(pyJsonDumps("\n\t\r\f\b")).toBe('"\\n\\t\\r\\f\\b"');
  });

  it("escapes other control characters as four hex digits", () => {
    expect(pyJsonDumps("\x00\x01\x1f")).toBe('"\\u0000\\u0001\\u001f"');
  });

  it("escapes DEL, which JSON.stringify leaves bare", () => {
    expect(pyJsonDumps("\x7f")).toBe('"\\u007f"');
  });

  it("does not escape the solidus", () => {
    expect(pyJsonDumps("/")).toBe('"/"');
  });

  it("escapes non-ASCII by default", () => {
    expect(pyJsonDumps("é")).toBe('"\\u00e9"');
    expect(pyJsonDumps("你好")).toBe('"\\u4f60\\u597d"');
  });

  it("escapes an astral character as a surrogate pair", () => {
    expect(pyJsonDumps("𝄞")).toBe('"\\ud834\\udd1e"');
  });

  it("emits non-ASCII literally when ensureAscii is off", () => {
    expect(pyJsonDumps("你好", { ensureAscii: false })).toBe('"你好"');
    expect(pyJsonDumps("\x7f", { ensureAscii: false })).toBe('"\x7f"');
  });

  it("still escapes control characters when ensureAscii is off", () => {
    expect(pyJsonDumps("\x00", { ensureAscii: false })).toBe('"\\u0000"');
  });
});

describe("pyJsonDumps numbers", () => {
  it("renders integers without a fractional part", () => {
    expect(pyJsonDumps(0)).toBe("0");
    expect(pyJsonDumps(-1)).toBe("-1");
    expect(pyJsonDumps(2 ** 53 - 1)).toBe("9007199254740991");
  });

  it("renders a large integer in full rather than in exponent form", () => {
    // String(1e21) is "1e+21"; CPython renders the integer 10**21 in full.
    expect(pyJsonDumps(1e21)).toBe("1000000000000000000000");
  });

  it("renders a non-integral value with its shortest round-trip form", () => {
    expect(pyJsonDumps(1.5)).toBe("1.5");
    expect(pyJsonDumps(0.1 + 0.2)).toBe("0.30000000000000004");
    expect(pyJsonDumps(1 / 3)).toBe("0.3333333333333333");
  });

  it("pads a negative exponent to two digits, as CPython does", () => {
    expect(pyJsonDumps(1e-7)).toBe("1e-07");
    expect(pyJsonDumps(1.5e-7)).toBe("1.5e-07");
  });

  it("keeps the sign on a negative value in exponent notation", () => {
    expect(pyJsonDumps(-1e-7)).toBe("-1e-07");
    expect(pyJsonDumps(-1.5e-7)).toBe("-1.5e-07");
  });

  it("keeps the sign on a negative value in fixed notation", () => {
    expect(pyJsonDumps(-0.5)).toBe("-0.5");
    expect(pyJsonDumps(-0.0001)).toBe("-0.0001");
  });

  it("uses fixed notation down to the CPython threshold", () => {
    expect(pyJsonDumps(0.0001)).toBe("0.0001");
    expect(pyJsonDumps(0.00001)).toBe("1e-05");
  });

  it("rejects a non-finite number, as CPython's allow_nan=False would", () => {
    expect(() => pyJsonDumps(Number.NaN)).toThrow(/not JSON serializable/);
    expect(() => pyJsonDumps(Number.POSITIVE_INFINITY)).toThrow(/not JSON serializable/);
  });
});

describe("pyJsonDumps literals", () => {
  it("renders the three JSON literals", () => {
    expect(pyJsonDumps(true)).toBe("true");
    expect(pyJsonDumps(false)).toBe("false");
    expect(pyJsonDumps(null)).toBe("null");
  });

  it("rejects a value with no JSON representation", () => {
    expect(() => pyJsonDumps(undefined)).toThrow(/not JSON serializable/);
    expect(() => pyJsonDumps(() => {})).toThrow(/not JSON serializable/);
  });
});

describe("differential parity with CPython", () => {
  it("matches every canonical rendering", () => {
    for (const record of golden.portable) {
      expect({ value: record.value, out: pyJsonDumps(record.value) }).toStrictEqual({
        value: record.value,
        out: record.canonical,
      });
    }
    expect(golden.portable.length).toBeGreaterThan(30);
  });

  it("matches every unsorted rendering that JavaScript can represent", () => {
    for (const record of golden.portable) {
      // JavaScript objects enumerate integer-like keys first, in ascending
      // numeric order, whatever the insertion order was. That record is
      // asserted separately as a divergence below.
      if (record.unsorted === '{"10":1,"9":2,"1":3}') {
        continue;
      }
      expect(pyJsonDumps(record.value, { sortKeys: false })).toBe(record.unsorted);
    }
  });

  it("documents that JavaScript cannot preserve insertion order for integer-like keys", () => {
    // Only the unsorted path is affected. The canonical payload sorts keys,
    // and a string sort of these keys is identical on both sides, so the
    // signature path is untouched.
    expect(pyJsonDumps({ "10": 1, "9": 2, "1": 3 }, { sortKeys: false })).toBe('{"1":3,"9":2,"10":1}');
    expect(pyJsonDumps({ "10": 1, "9": 2, "1": 3 })).toBe('{"1":3,"10":1,"9":2}');
    const record = golden.portable.find((entry) => entry.unsorted === '{"10":1,"9":2,"1":3}');
    expect(record?.canonical).toBe('{"1":3,"10":1,"9":2}');
  });

  it("matches every non-escaping rendering", () => {
    for (const record of golden.portable) {
      expect(pyJsonDumps(record.value, { ensureAscii: false })).toBe(record.unicode);
    }
  });

  it("documents where a JavaScript number cannot carry Python's int/float split", () => {
    // JavaScript has one number type, so an integral value is indistinguishable
    // from a Python int and renders the way CPython renders an int. Realistic
    // identity claims hold strings, ints and booleans, so this does not bite in
    // practice — but a claim holding a whole-valued float would sign
    // differently, and that has to be visible rather than surprising.
    const divergences = golden.float_divergences.map((record) => record.canonical);
    expect(divergences).toStrictEqual([
      "0.0",
      "1.0",
      "-0.0",
      "-1.0",
      "1e+21",
      "1e-07",
      "1e+16",
      '{"whole":2.0}',
      "[1.0,1.5]",
    ]);
    // -0.0 collapses too: it is integral, so it takes the int path, and
    // CPython renders the int 0 without a sign.
    expect([pyJsonDumps(0.0), pyJsonDumps(1.0), pyJsonDumps(-0.0), pyJsonDumps(-1.0)]).toStrictEqual([
      "0",
      "1",
      "0",
      "-1",
    ]);
    expect(pyJsonDumps(1e16)).toBe("10000000000000000");
    expect(pyJsonDumps({ whole: 2.0 })).toBe('{"whole":2}');
    // A non-integral float in the same array still matches CPython exactly.
    expect(pyJsonDumps([1.0, 1.5])).toBe("[1,1.5]");
    // And CPython agrees with this port whenever the value really is an int.
    expect(pyJsonDumps(1e21)).toBe("1000000000000000000000");
  });
});
