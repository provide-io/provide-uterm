//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { resolveSecurityHeaders, SECURITY_HEADER_FIELDS, type SecurityHeaderConfig } from "./index.ts";

interface HeadersGolden {
  default_mode: string;
  header_order: string[];
  resolved: Array<{ name: string; mode: string; overrides: Record<string, string>; headers: string[][] }>;
  schema_default: string[][];
}

const golden = loadGolden<HeadersGolden>("securityheaders_golden.json");

/** A configuration with a mode and any overrides. */
function config(mode: string, overrides: Record<string, string> = {}): SecurityHeaderConfig {
  return { mode, ...overrides } as SecurityHeaderConfig;
}

describe("resolving the response headers", () => {
  it.each(golden.resolved)("$name", (record) => {
    expect(resolveSecurityHeaders(config(record.mode, record.overrides)).map((pair) => [...pair])).toEqual(
      record.headers,
    );
  });

  it("carries the whole set in strict mode", () => {
    const names = resolveSecurityHeaders(config("strict")).map(([name]) => name);
    expect(names).toEqual(golden.header_order);
  });

  it("keeps only nosniff in dev mode", () => {
    // Content sniffing is not a development convenience — it is a bug in a
    // browser that a page cannot work around.
    expect(resolveSecurityHeaders(config("dev"))).toEqual([["X-Content-Type-Options", "nosniff"]]);
  });

  it("treats any mode that is not strict as relaxed", () => {
    // A deployment that misspells the mode gets a visible relaxation rather
    // than a silent strictness it did not ask for.
    for (const mode of ["dev", "prod", "production", "nonsense", ""]) {
      expect(resolveSecurityHeaders(config(mode))).toHaveLength(1);
    }
  });

  it("compares the mode exactly, where the posture report normalises", () => {
    // A cross-module inconsistency in the reference, pinned rather than
    // smoothed over: `security.mode` is lowercased and trimmed when the
    // posture report reads it, and compared verbatim here. A config writing
    // `STRICT` therefore reports as strict and serves the dev headers.
    expect(resolveSecurityHeaders(config("STRICT"))).toHaveLength(1);
    expect(resolveSecurityHeaders(config(" strict "))).toHaveLength(1);
    expect(resolveSecurityHeaders(config("strict"))).toHaveLength(golden.header_order.length);
  });

  it("lets an override replace a default", () => {
    const headers = resolveSecurityHeaders(config("strict", { csp: "default-src 'none'" }));
    expect(headers[0]).toEqual(["Content-Security-Policy", "default-src 'none'"]);
  });

  it("lets an empty override suppress a header", () => {
    // Different from an absent one, and the config expresses them
    // differently: a deployment behind a proxy that already sets a policy has
    // to be able to turn one off without turning them all off.
    const names = resolveSecurityHeaders(config("strict", { csp: "" })).map(([name]) => name);
    expect(names).not.toContain("Content-Security-Policy");
    expect(names).toHaveLength(golden.header_order.length - 1);
  });

  it("lets an override add a header the mode does not carry", () => {
    const names = resolveSecurityHeaders(config("dev", { hsts: "max-age=1" })).map(([name]) => name);
    expect(names).toEqual(["Strict-Transport-Security", "X-Content-Type-Options"]);
  });

  it("treats an explicit null as unset, not as a value", () => {
    // The schema declares each override as a string *or* null, so a config
    // can carry one. Read as a value it would put the text "null" into the
    // policy header, which a browser would enforce.
    const headers = resolveSecurityHeaders({ mode: "strict", csp: null });
    expect(headers[0]?.[1]).toBe(resolveSecurityHeaders({ mode: "strict" })[0]?.[1]);
    expect(headers.map(([, value]) => value)).not.toContain("null");
  });

  it("does not treat whitespace as empty", () => {
    // Only the empty string suppresses; a value of one space is a value.
    const headers = resolveSecurityHeaders(config("strict", { csp: " " }));
    expect(headers[0]).toEqual(["Content-Security-Policy", " "]);
  });

  it("can be turned off entirely", () => {
    const every = Object.fromEntries(SECURITY_HEADER_FIELDS.map(([field]) => [field, ""]));
    expect(resolveSecurityHeaders(config("strict", every))).toEqual([]);
  });

  it("emits them in a stable order", () => {
    // They go onto every response, so the order is part of what a test or a
    // proxy downstream sees.
    expect(resolveSecurityHeaders(config("strict")).map(([name]) => name)).toEqual(golden.header_order);
    expect(SECURITY_HEADER_FIELDS.map(([, header]) => header)).toEqual(golden.header_order);
  });

  it("matches the schema's own default configuration", () => {
    expect(golden.default_mode).toBe("strict");
    expect(resolveSecurityHeaders(config(golden.default_mode)).map((pair) => [...pair])).toEqual(golden.schema_default);
  });
});
