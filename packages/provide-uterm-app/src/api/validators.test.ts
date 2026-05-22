//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import {
  parseHttpRequestEntry,
  parseHttpResponseEntry,
  parseRawRecordingEntries,
  parseRawSessionStatus,
  parseRawSessionStatusList,
  ValidationError,
} from "./validators";

const VALID_SESSION = {
  session_id: "s1",
  display_name: "S1",
  connector_type: "pty",
  lifecycle_state: "running",
  input_mode: "open",
  connected: true,
  auto_start: false,
  tags: ["a"],
  recording_enabled: false,
  recording_available: false,
  owner: null,
  visibility: "public",
  last_error: null,
};

describe("parseRawSessionStatus", () => {
  it("accepts a valid payload", () => {
    const r = parseRawSessionStatus(VALID_SESSION);
    expect(r.session_id).toBe("s1");
    expect(r.tags).toEqual(["a"]);
  });

  it("defaults visibility to 'public' when missing", () => {
    const { visibility: _v, ...rest } = VALID_SESSION;
    const r = parseRawSessionStatus(rest);
    expect(r.visibility).toBe("public");
  });

  it("accepts owner as string", () => {
    const r = parseRawSessionStatus({ ...VALID_SESSION, owner: "u1" });
    expect(r.owner).toBe("u1");
  });

  it("accepts last_error as string", () => {
    const r = parseRawSessionStatus({ ...VALID_SESSION, last_error: "boom" });
    expect(r.last_error).toBe("boom");
  });

  it("rejects non-object input", () => {
    expect(() => parseRawSessionStatus(null)).toThrow(ValidationError);
    expect(() => parseRawSessionStatus([])).toThrow(ValidationError);
    expect(() => parseRawSessionStatus("nope")).toThrow(ValidationError);
  });

  it("rejects missing string field", () => {
    const { session_id: _, ...rest } = VALID_SESSION;
    expect(() => parseRawSessionStatus(rest)).toThrow(/session_id/);
  });

  it("rejects wrong-type boolean field", () => {
    expect(() => parseRawSessionStatus({ ...VALID_SESSION, connected: "yes" })).toThrow(/connected/);
  });

  it("rejects non-array tags", () => {
    expect(() => parseRawSessionStatus({ ...VALID_SESSION, tags: "a,b" })).toThrow(/tags/);
  });

  it("rejects non-string tag element", () => {
    expect(() => parseRawSessionStatus({ ...VALID_SESSION, tags: ["a", 7] })).toThrow(/tags\[1\]/);
  });

  it("rejects wrong-type nullable string", () => {
    expect(() => parseRawSessionStatus({ ...VALID_SESSION, owner: 42 })).toThrow(/owner/);
  });
});

describe("parseRawSessionStatusList", () => {
  it("parses a list", () => {
    expect(parseRawSessionStatusList([VALID_SESSION])).toHaveLength(1);
  });

  it("rejects non-array", () => {
    expect(() => parseRawSessionStatusList({})).toThrow(ValidationError);
  });

  it("includes index in error path", () => {
    expect(() => parseRawSessionStatusList([VALID_SESSION, { ...VALID_SESSION, session_id: 1 }])).toThrow(
      /sessions\[1\]\.session_id/,
    );
  });
});

describe("parseRawRecordingEntries", () => {
  it("parses entries with all fields", () => {
    const r = parseRawRecordingEntries([{ ts: 1, event: "output", data: { screen: "x" } }]);
    expect(r[0]).toEqual({ ts: 1, event: "output", data: { screen: "x" } });
  });

  it("parses entries with no optional fields", () => {
    expect(parseRawRecordingEntries([{}])).toEqual([{}]);
  });

  it("rejects non-array input", () => {
    expect(() => parseRawRecordingEntries("nope")).toThrow(ValidationError);
  });

  it("rejects non-object entry", () => {
    expect(() => parseRawRecordingEntries([null])).toThrow(/entries\[0\]/);
  });

  it("rejects wrong-type ts", () => {
    expect(() => parseRawRecordingEntries([{ ts: "1" }])).toThrow(/ts/);
  });

  it("rejects wrong-type event", () => {
    expect(() => parseRawRecordingEntries([{ event: 7 }])).toThrow(/event/);
  });

  it("rejects wrong-type data", () => {
    expect(() => parseRawRecordingEntries([{ data: "x" }])).toThrow(/data/);
  });
});

const VALID_REQ = {
  type: "http_req",
  id: "r1",
  ts: 1,
  method: "GET",
  url: "/x",
  headers: { "x-h": "v" },
  body_size: 0,
};

describe("parseHttpRequestEntry", () => {
  it("parses a minimal valid request", () => {
    const r = parseHttpRequestEntry(VALID_REQ);
    expect(r.id).toBe("r1");
    expect(r.body_b64).toBeUndefined();
  });

  it("preserves optional flags when present", () => {
    const r = parseHttpRequestEntry({
      ...VALID_REQ,
      body_b64: "abc",
      body_truncated: true,
      body_binary: false,
      intercepted: true,
    });
    expect(r.body_b64).toBe("abc");
    expect(r.body_truncated).toBe(true);
    expect(r.body_binary).toBe(false);
    expect(r.intercepted).toBe(true);
  });

  it("rejects wrong type discriminator", () => {
    expect(() => parseHttpRequestEntry({ ...VALID_REQ, type: "http_res" })).toThrow(/type/);
  });

  it("rejects non-object", () => {
    expect(() => parseHttpRequestEntry(null)).toThrow(ValidationError);
  });

  it("rejects missing headers", () => {
    const { headers: _h, ...rest } = VALID_REQ;
    expect(() => parseHttpRequestEntry(rest)).toThrow(/headers/);
  });

  it("rejects non-string header value", () => {
    expect(() => parseHttpRequestEntry({ ...VALID_REQ, headers: { "x-h": 1 } })).toThrow(/headers/);
  });

  it("rejects NaN number", () => {
    expect(() => parseHttpRequestEntry({ ...VALID_REQ, ts: Number.NaN })).toThrow(/ts/);
  });
});

const VALID_RES = {
  type: "http_res",
  id: "r1",
  ts: 1,
  status: 200,
  status_text: "OK",
  headers: {},
  body_size: 0,
  duration_ms: 5,
};

describe("parseHttpResponseEntry", () => {
  it("parses a minimal valid response", () => {
    const r = parseHttpResponseEntry(VALID_RES);
    expect(r.status).toBe(200);
  });

  it("preserves optional body flags", () => {
    const r = parseHttpResponseEntry({
      ...VALID_RES,
      body_b64: "abc",
      body_truncated: true,
      body_binary: true,
    });
    expect(r.body_b64).toBe("abc");
    expect(r.body_truncated).toBe(true);
    expect(r.body_binary).toBe(true);
  });

  it("rejects wrong type discriminator", () => {
    expect(() => parseHttpResponseEntry({ ...VALID_RES, type: "http_req" })).toThrow(/type/);
  });

  it("rejects non-object", () => {
    expect(() => parseHttpResponseEntry(42)).toThrow(ValidationError);
  });
});

describe("ValidationError", () => {
  it("preserves path and reason fields", () => {
    const err = new ValidationError("a.b", "bad");
    expect(err.path).toBe("a.b");
    expect(err.reason).toBe("bad");
    expect(err.name).toBe("ValidationError");
    expect(err.message).toContain("a.b");
    expect(err.message).toContain("bad");
  });
});
