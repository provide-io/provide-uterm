//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { BINARY_CONTENT_TYPES, BODY_MAX_BYTES, encodeBody, formatLogLine } from "./index.ts";

interface ProxyGolden {
  body_max_bytes: number;
  binary_types: string[];
  bodies: Array<{
    name: string;
    size: number;
    content_type: string;
    encoded: Record<string, unknown>;
  }>;
  logs: Array<{
    name: string;
    method: string;
    url: string;
    status: number | null;
    duration_ms: number | null;
    body_size: number;
    line: string;
  }>;
}

const golden = loadGolden<ProxyGolden>("httpproxy_golden.json");

/** A body of `size` bytes, since the corpus records only the size. */
function bodyOf(size: number, name: string): Uint8Array {
  if (name === "bytes that are not text") {
    return Uint8Array.from([0xff, 0xfe, 0x00]);
  }
  if (name === "a little text" || name === "text with no type given") {
    return new TextEncoder().encode("hello");
  }
  return new Uint8Array(size).fill("x".charCodeAt(0));
}

describe("what a body is reported as", () => {
  it.each(golden.bodies)("$name", (record) => {
    const encoded = encodeBody(bodyOf(record.size, record.name), record.content_type);
    // Compared by key as well as value: a field present and undefined is not
    // the same as a field absent, and the wire format is the keys.
    expect(Object.keys(encoded).sort()).toEqual(Object.keys(record.encoded).sort());
    for (const [key, value] of Object.entries(record.encoded)) {
      if (key === "body_b64") {
        expect(typeof encoded.body_b64).toBe("string");
        continue;
      }
      expect(encoded[key as keyof typeof encoded]).toEqual(value);
    }
  });

  it("always says the size, whatever else it says", () => {
    // Which is what an operator scans a list for.
    for (const [body, type] of [
      [new Uint8Array(0), "text/plain"],
      [new TextEncoder().encode("hi"), "text/plain"],
      [new Uint8Array(10), "image/png"],
      [new Uint8Array(BODY_MAX_BYTES + 1), "text/plain"],
    ] as const) {
      expect(encodeBody(body, type).body_size).toBe(body.length);
    }
  });

  it("reports something binary rather than carrying it", () => {
    // A megabyte of base64 nobody can read is a megabyte spent.
    const encoded = encodeBody(new TextEncoder().encode("hello"), "image/png");
    expect(encoded.body_binary).toBe(true);
    expect(encoded.body_b64).toBeUndefined();
  });

  it("marks something too large rather than carrying it", () => {
    const encoded = encodeBody(new Uint8Array(BODY_MAX_BYTES + 1), "text/plain");
    expect(encoded.body_truncated).toBe(true);
    expect(encoded.body_b64).toBeUndefined();
  });

  it("carries a body of exactly the limit", () => {
    // The limit is what fits, not what is one too many.
    expect(encodeBody(new Uint8Array(BODY_MAX_BYTES), "text/plain").body_b64).toBeDefined();
    expect(encodeBody(new Uint8Array(BODY_MAX_BYTES + 1), "text/plain").body_b64).toBeUndefined();
  });

  it("says nothing but the size about an empty body", () => {
    // Not even that it was binary: there is nothing there to be binary.
    for (const type of ["text/plain", "image/png", ""]) {
      expect(Object.keys(encodeBody(new Uint8Array(0), type))).toEqual(["body_size"]);
    }
  });

  it("reads the type without its parameters", () => {
    // `text/html; charset=utf-8` is the same type as `text/html`.
    for (const type of ["image/png", "image/png; x=1", "  image/png ; x=1  ", "image/png;"]) {
      expect(encodeBody(new TextEncoder().encode("hi"), type).body_binary).toBe(true);
    }
  });

  it("reads the type however it was capitalised", () => {
    for (const type of ["IMAGE/PNG", "Image/Png", "image/PNG"]) {
      expect(encodeBody(new TextEncoder().encode("hi"), type).body_binary).toBe(true);
    }
  });

  it("matches a type by its start, not anywhere in it", () => {
    // `x-image/png` is not an image type, and treating it as one would hide a
    // body somebody wanted to read.
    for (const type of ["x-image/png", "text/x-image", "application/json"]) {
      expect(encodeBody(new TextEncoder().encode("hi"), type).body_binary).toBeUndefined();
    }
  });

  it("carries text with no type given", () => {
    // An absent type is not a reason to withhold a readable body.
    expect(encodeBody(new TextEncoder().encode("hi"), "").body_b64).toBeDefined();
  });

  it("knows the same types the reference knows", () => {
    expect([...BINARY_CONTENT_TYPES].sort()).toEqual(golden.binary_types);
    expect(BODY_MAX_BYTES).toBe(golden.body_max_bytes);
  });

  it("carries a body that is not text, when its type says it is", () => {
    // The type decides, not the bytes: a caller that mislabels its body gets
    // the body it asked for.
    const encoded = encodeBody(Uint8Array.from([0xff, 0xfe, 0x00]), "text/plain");
    expect(encoded.body_b64).toBe(Buffer.from([0xff, 0xfe, 0x00]).toString("base64"));
  });
});

describe("one line for an exchange", () => {
  it.each(golden.logs)("$name", (record) => {
    expect(
      formatLogLine(
        record.method,
        record.url,
        record.status ?? undefined,
        record.duration_ms ?? undefined,
        record.body_size,
      ),
    ).toBe(record.line);
  });

  it("says which direction it is without anything being parsed", () => {
    expect(formatLogLine("GET", "https://x/", undefined, undefined, 0).startsWith("→")).toBe(true);
    expect(formatLogLine("GET", "https://x/", 200, 1, 0).startsWith("←")).toBe(true);
  });

  it("marks a failure at the far end, and only that", () => {
    // Which is the line somebody scanning is looking for.
    for (const status of [500, 502, 503, 599]) {
      expect(formatLogLine("GET", "https://x/", status, 1, 0).endsWith("⚠")).toBe(true);
    }
    for (const status of [200, 302, 404, 499]) {
      expect(formatLogLine("GET", "https://x/", status, 1, 0).endsWith("⚠")).toBe(false);
    }
  });

  it("says a duration it does not know rather than nothing", () => {
    expect(formatLogLine("GET", "https://x/", 200, undefined, 0)).toContain("(?,");
  });

  it("rounds a duration the way the reference rounds it", () => {
    // To even, so 12.5 and 11.5 are both 12. A line that differs between two
    // runtimes is a line somebody has to reconcile.
    expect(formatLogLine("GET", "https://x/", 200, 12.5, 0)).toContain("12ms");
    expect(formatLogLine("GET", "https://x/", 200, 11.5, 0)).toContain("12ms");
    expect(formatLogLine("GET", "https://x/", 200, 12.6, 0)).toContain("13ms");
    expect(formatLogLine("GET", "https://x/", 200, 0, 0)).toContain("0ms");
  });

  it("sizes a body the way somebody reads it", () => {
    for (const [size, shown] of [
      [0, "0B"],
      [1, "1B"],
      [1023, "1023B"],
      [1024, "1.0KB"],
      [1536, "1.5KB"],
      [1024 * 1024 - 1, "1024.0KB"],
      [1024 * 1024, "1.0MB"],
      [5 * 1024 * 1024 + 512 * 1024, "5.5MB"],
    ] as const) {
      expect(formatLogLine("GET", "https://x/", 200, 1, size)).toContain(`, ${shown})`);
    }
  });

  it("names the method and the address it was given", () => {
    const line = formatLogLine("DELETE", "https://example.test/a/b?c=d", 204, 3, 0);
    expect(line).toContain("DELETE");
    expect(line).toContain("https://example.test/a/b?c=d");
    expect(line).toContain("204");
  });
});
