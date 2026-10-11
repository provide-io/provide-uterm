//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A file served as the reference's framework serves it.
 *
 * The expected answers are the reference server's own: every download probe
 * in `serverrecording_golden.json`, which `gen_serverrecording_golden.py`
 * records off a running reference serving a fixture file whose modification
 * time is pinned. The same fixture, at the same instant, is served here.
 */

import { existsSync, mkdtempSync, readdirSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { fileResponse, MAX_RANGES, pyMtime } from "./file-response.ts";

interface Probe {
  id: string;
  auth: string;
  request_headers: Record<string, string>;
  status: number;
  headers: Record<string, string>;
  body: unknown;
}

interface RecordingGolden {
  fixture: string[];
  fixture_mtime_ns: number;
  boundary: string;
  probes: Probe[];
}

const golden = loadGolden<RecordingGolden>("serverrecording_golden.json");

/** A boundary as long as Starlette's thirteen random bytes in hex. */
const BOUNDARY = "0123456789abcdef0123456789"; // pragma: allowlist secret

/** The download probes that reached the file, rather than a refusal before it. */
const served = golden.probes.filter((probe) => probe.id.startsWith("download") && probe.auth === "token");
const whole = served.find((probe) => probe.id === "download") as Probe;

let directory: string;
let fixture: string;

beforeAll(() => {
  directory = mkdtempSync(join(tmpdir(), "uterm-file-"));
  fixture = join(directory, "recorded.jsonl");
  writeFileSync(fixture, golden.fixture.map((line) => `${line}\n`).join(""));
  const seconds = golden.fixture_mtime_ns / 1e9;
  utimesSync(fixture, seconds, seconds);
});

afterAll(() => {
  rmSync(directory, { recursive: true, force: true });
});

/** A probe's request headers, with the validators it quotes filled in. */
function requestHeaders(probe: Probe): Headers {
  const headers = new Headers();
  for (const [name, value] of Object.entries(probe.request_headers)) {
    headers.set(name, value.startsWith("<") ? (whole.headers[value.slice(1, -1)] as string) : value);
  }
  return headers;
}

/** The headers the golden kept, from a response, with the boundary replaced. */
function keptHeaders(response: Response, names: readonly string[]): Record<string, string> {
  const kept: Record<string, string> = {};
  for (const name of names) {
    const value = response.headers.get(name);
    if (value !== null) {
      kept[name] = value.replace(BOUNDARY, golden.boundary);
    }
  }
  return kept;
}

const KEPT = [
  "content-type",
  "allow",
  "content-disposition",
  "content-length",
  "accept-ranges",
  "content-range",
  "etag",
  "last-modified",
];

describe("serving the fixture as the reference served it", () => {
  it("covers every download the reference answered with the file or a range refusal", () => {
    expect(served.filter((probe) => probe.status !== 404 && probe.status !== 422).length).toBe(19);
  });

  for (const probe of served.filter((one) => one.status !== 404 && one.status !== 422)) {
    it(`${probe.id}: ${JSON.stringify(probe.request_headers)}`, async () => {
      const response = fileResponse(fixture, {
        filename: "recorded.jsonl",
        mediaType: "application/json",
        requestHeaders: requestHeaders(probe),
        boundary: () => BOUNDARY,
      });
      expect(response.status).toBe(probe.status);
      expect(keptHeaders(response, KEPT)).toStrictEqual(probe.headers);
      expect((await response.text()).replaceAll(BOUNDARY, golden.boundary)).toBe(probe.body);
    });
  }
});

describe("the parts of the port no reference probe reaches", () => {
  it("draws a random boundary of Starlette's length when nobody pins one", async () => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range: "bytes=0-0,5-5" }),
    });
    expect(response.headers.get("content-type")).toMatch(/^multipart\/byteranges; boundary=[0-9a-f]{26}$/);
  });

  it("names a file it cannot quote plainly in the RFC 5987 form", () => {
    const response = fileResponse(fixture, {
      filename: "café one.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers(),
    });
    expect(response.headers.get("content-disposition")).toBe("attachment; filename*=utf-8''caf%C3%A9%20one.jsonl");
  });

  it("skips a part whose end is not a number, and one with a negative start", async () => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range: "bytes=0-x,-x,2-3" }),
    });
    expect(response.status).toBe(206);
    expect(response.headers.get("content-range")).toBe(`bytes 2-3/${whole.headers["content-length"]}`);
  });

  it("refuses an end before its start once Python's int() has read a negative", async () => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range: "bytes=5--3" }),
    });
    expect(response.status).toBe(400);
    expect(await response.text()).toBe("Range header: start must be less than end");
  });

  it("merges ranges that start together, as sorting the tuples does", async () => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range: "bytes=0-5,0-3" }),
    });
    expect(response.headers.get("content-range")).toBe(`bytes 0-5/${whole.headers["content-length"]}`);
  });

  // Expected ranges below are Starlette's own, from FileResponse._parse_range_header
  // on an 888-byte file, recorded with `uv run python`.
  it.each([
    ["bytes=x0-1,0-1x,3-4", "bytes 3-4/888"],
    ["bytes=1_0-2_0", "bytes 10-20/888"],
    ["bytes=" + Array(100).fill("0-0").join(","), "bytes 0-0/888"],
    ["bytes=10-11,0-20", "bytes 0-20/888"],
    ["bytes=0-888", "bytes 0-887/888"],
  ])("serves %j as the one range Starlette serves", (range, contentRange) => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range }),
    });
    expect(response.status).toBe(206);
    expect(response.headers.get("content-range")).toBe(contentRange);
  });

  it("reads a suffix written after a space, as stripping each part allows", async () => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range: "bytes=0-1, -5" }),
      boundary: () => BOUNDARY,
    });
    const body = await response.text();
    expect(body).toContain("Content-Range: bytes 0-1/888");
    expect(body).toContain("Content-Range: bytes 883-887/888");
  });

  it.each(["bytes=888-", "bytes=0-1,5000-", "bytes=--5"])("refuses %j as unsatisfiable", (range) => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range }),
    });
    expect(response.status).toBe(416);
  });

  it.each(["bytes=2--3", "bytes=3-2", "bytes=0-1,5-2"])("refuses %j as a start not before its end", async (range) => {
    const response = fileResponse(fixture, {
      filename: "recorded.jsonl",
      mediaType: "application/json",
      requestHeaders: new Headers({ range }),
    });
    expect(response.status).toBe(400);
    expect(await response.text()).toBe("Range header: start must be less than end");
  });

  it("throws ENOENT for a path that is not there and EISDIR for a directory", () => {
    const options = { filename: "x", mediaType: "application/json", requestHeaders: new Headers() };
    expect(() => fileResponse(join(directory, "missing.jsonl"), options)).toThrow(
      expect.objectContaining({ code: "ENOENT" }),
    );
    expect(() => fileResponse(directory, options)).toThrow(expect.objectContaining({ code: "EISDIR" }));
  });

  it.skipIf(!existsSync("/proc/self/fd"))("closes the descriptor it opened, whether the read succeeds or fails", () => {
    const options = { filename: "x", mediaType: "application/json", requestHeaders: new Headers() };
    const before = readdirSync("/proc/self/fd").length;
    for (let i = 0; i < 20; i++) {
      fileResponse(fixture, options);
      expect(() => fileResponse(directory, options)).toThrow();
    }
    expect(readdirSync("/proc/self/fd").length).toBe(before);
  });

  it("caps the number of ranges where Starlette does", () => {
    expect(MAX_RANGES).toBe(100);
  });

  it("builds st_mtime as CPython does, from the seconds and the nanoseconds", () => {
    // CPython's `sec + nsec * 1e-9`.
    expect(pyMtime(1_700_000_000_123_456_789n)).toBe(1_700_000_000 + 123_456_789 * 1e-9);
    expect(pyMtime(1_700_000_000_500_000_000n)).toBe(1_700_000_000.5);
  });
});
