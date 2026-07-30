//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { TokenBucket } from "../ratelimit/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  type ConfigError,
  coerceSection,
  SECTION_FIELD_SPECS,
  SERVER_CONFIG_DEFAULTS,
  TOP_LEVEL_FIELD_SPEC,
  validateSection,
  validateServerConfig,
} from "./index.ts";

interface FieldSpec {
  kind: string;
  optional: boolean;
  required: boolean;
  choices?: string[];
  item?: { kind: string; name?: string };
  name?: string;
}

interface Case {
  model: string;
  name: string;
  kwargs: Record<string, unknown>;
  errors?: ConfigError[];
  accepted?: Record<string, unknown>;
}

interface SchemaGolden {
  specs: Record<string, Record<string, FieldSpec>>;
  top_level_spec: Record<string, FieldSpec>;
  cases: Case[];
  top_level_cases: Omit<Case, "model">[];
}

const golden = loadGolden<SchemaGolden>("configschema_golden.json");

/** The shorthand in the spec table, expanded into what the corpus records. */
function expand(code: string | readonly string[]): FieldSpec {
  if (Array.isArray(code)) {
    return { kind: "literal", optional: false, required: false, choices: [...code] };
  }
  let text = code as string;
  const required = text.endsWith("!");
  if (required) {
    text = text.slice(0, -1);
  }
  const optional = text.endsWith("?");
  if (optional) {
    text = text.slice(0, -1);
  }
  if (text.endsWith("[]")) {
    const inner = text.slice(0, -2);
    const item = inner.startsWith("model:") ? { kind: "model", name: inner.slice(6) } : { kind: inner };
    return { kind: "list", optional, required, item };
  }
  if (text.startsWith("model:")) {
    return { kind: "model", optional, required, name: text.slice(6) };
  }
  return { kind: text, optional, required };
}

describe("the schema the port carries", () => {
  it.each(Object.keys(golden.specs))("%s has the fields the model has", (model) => {
    const ported = SECTION_FIELD_SPECS[model as keyof typeof SECTION_FIELD_SPECS] as Record<string, string>;
    expect(Object.fromEntries(Object.entries(ported).map(([name, code]) => [name, expand(code)]))).toEqual(
      golden.specs[model],
    );
  });

  it("has the top-level fields the model has", () => {
    expect(
      Object.fromEntries(Object.entries(TOP_LEVEL_FIELD_SPEC).map(([name, code]) => [name, expand(code)])),
    ).toEqual(golden.top_level_spec);
  });

  it("names every section the document references", () => {
    for (const spec of Object.values(golden.top_level_spec)) {
      const referenced = spec.kind === "model" ? spec.name : spec.item?.name;
      if (referenced !== undefined) {
        expect(SECTION_FIELD_SPECS).toHaveProperty(referenced);
      }
    }
  });
});

describe("what a section accepts", () => {
  it.each(golden.cases)("$model: $name", (record) => {
    expect(validateSection(record.model, record.kwargs)).toEqual(record.errors ?? []);
  });

  it.each(
    golden.cases.filter((record) => record.accepted !== undefined),
  )("$model: $name — coerced as the reference coerced it", (record) => {
    const coerced = coerceSection(record.model, record.kwargs);
    for (const field of Object.keys(record.kwargs)) {
      expect(coerced[field]).toEqual(record.accepted?.[field]);
    }
  });

  it("refuses a name nobody defined", () => {
    // Every section forbids extras, so a typo is a startup failure rather than
    // a setting that silently does nothing — including a security setting the
    // operator believes is on.
    expect(validateSection("SecurityConfig", { metrics_require_authh: true })).toEqual([
      { type: "extra_forbidden", loc: ["metrics_require_authh"], msg: "Extra inputs are not permitted" },
    ]);
  });

  it("refuses a value outside a closed set rather than falling back", () => {
    // Every one of these enumerations names a less safe option alongside its
    // default, so falling back would pick one without saying so.
    for (const [section, field, value] of [
      ["SecurityConfig", "mode", "relaxed"],
      ["AuthConfig", "identity_provider", "ldap"],
      ["AuthConfig", "webhook_idp_on_failure", "allow"],
      ["ControlPlaneConfig", "backend", "postgres"],
      ["PamConfig", "mode", "record"],
    ] as const) {
      const errors = validateSection(section, { [field]: value });
      expect(errors).toHaveLength(1);
      expect(errors[0]?.type).toBe("literal_error");
      expect(errors[0]?.loc).toEqual([field]);
    }
  });

  it("matches a choice exactly, not by case", () => {
    expect(validateSection("SecurityConfig", { mode: "STRICT" })[0]?.type).toBe("literal_error");
    expect(validateSection("TunnelConfig", { cookie_samesite: "Lax" })[0]?.type).toBe("literal_error");
  });

  it("reports every bad field, not only the first", () => {
    expect(validateSection("SecurityConfig", { mode: "relaxed", metrics_require_auth: [] })).toHaveLength(2);
  });

  it("reports them in the order the model declares them", () => {
    // Not the order the document happens to write them in, so two operators
    // with the same mistakes read the same report.
    const written = validateSection("SecurityConfig", { metrics_require_auth: [], mode: "relaxed" });
    expect(written.map((error) => error.loc[0])).toEqual(["mode", "metrics_require_auth"]);
  });

  it("reports a name nobody defined after the fields that exist", () => {
    const errors = validateSection("SecurityConfig", { metrics_require_authh: true, mode: "relaxed" });
    expect(errors.map((error) => error.type)).toEqual(["literal_error", "extra_forbidden"]);
  });

  it("does not run the cross-field rules when a field is already wrong", () => {
    // As the reference does not: the combination is checked against values
    // that were accepted, so a rule cannot be handed a value the field
    // refused.
    const errors = validateSection("AuthConfig", {
      identity_provider: "webhook",
      webhook_idp_require_signed_response: true,
      clock_skew_seconds: "x",
    });
    expect(errors.map((error) => error.type)).toEqual(["int_parsing"]);
  });

  it("reports a cross-field rule against the section, not a field", () => {
    const errors = validateSection("AuditConfig", { chain_enabled: true });
    expect(errors).toHaveLength(1);
    expect(errors[0]?.loc).toEqual([]);
    expect(errors[0]?.type).toBe("value_error");
  });

  it("refuses a cleartext outbound URL in every section that has one", () => {
    // They carry HMAC secrets, auth headers, and the keys used to validate
    // admin tokens.
    for (const [section, field] of [
      ["AuthConfig", "webhook_idp_url"],
      ["AuthConfig", "jwt_jwks_url"],
      ["RecordingConfig", "webhook_url"],
      ["PamConfig", "relay_url"],
      ["GovernanceConfig", "policy_webhook_url"],
      ["GovernanceConfig", "registry_webhook_url"],
      ["GovernanceConfig", "authz_webhook_url"],
      ["GovernanceConfig", "behavioral_audit_url"],
      ["GovernanceConfig", "telemetry_webhook_url"],
    ] as const) {
      expect(validateSection(section, { [field]: "http://elsewhere.example/hook" })).toHaveLength(1);
      expect(validateSection(section, { [field]: "https://elsewhere.example/hook" })).toEqual([]);
    }
  });

  it("refuses a section nobody defined", () => {
    // A caller's bug rather than an operator's, so it is raised rather than
    // reported — and it names the section, because the caller passed a string.
    expect(() => validateSection("NoSuchConfig", {})).toThrow("no such configuration section: NoSuchConfig");
  });

  it("refuses a table given a null", () => {
    // `typeof null` is `"object"`, so a check that only asks whether this is
    // an object would take a null as an empty table and accept every default.
    expect(validateSection("GraphicalTargetConfig", { config: null })[0]?.type).toBe("dict_type");
    expect(validateServerConfig({ auth: null })[0]?.type).toBe("model_type");
  });

  it("refuses a sqlite control plane with nowhere to store", () => {
    const errors = validateSection("ControlPlaneConfig", { backend: "sqlite" });
    expect(errors).toHaveLength(1);
    expect(errors[0]?.loc).toEqual([]);
    expect(validateSection("ControlPlaneConfig", { backend: "sqlite", database_url: "sqlite:///x.db" })).toEqual([]);
  });

  it("accepts a boolean written as 0, not only as 1", () => {
    // `coerceBool`'s numeric branch is `value === 0 || value === 1`; a test
    // that only ever wrote 1 (or any other number) could not tell "reads 0 as
    // false" apart from "refuses every number that isn't 1".
    expect(validateSection("AuthConfig", { api_keys_enabled: 0 })).toEqual([]);
    expect(coerceSection("AuthConfig", { api_keys_enabled: 0 }).api_keys_enabled).toBe(false);
  });

  it("trims a boolean word before matching it", () => {
    expect(coerceSection("AuthConfig", { api_keys_enabled: " true " }).api_keys_enabled).toBe(true);
  });

  it("accepts an explicit plus sign on an integer", () => {
    // The sign class is `[+-]?`; a test that only ever wrote a bare or a
    // minus-signed integer would not notice the plus sign specifically
    // dropping out of the class.
    expect(validateSection("AuthConfig", { clock_skew_seconds: "+5" })).toEqual([]);
    expect(coerceSection("AuthConfig", { clock_skew_seconds: "+5" }).clock_skew_seconds).toBe(5);
  });

  it("trims a float string before matching and parsing it", () => {
    expect(coerceSection("AuthConfig", { webhook_idp_timeout_s: " 5.5 " }).webhook_idp_timeout_s).toBe(5.5);
  });

  it("matches a float end to end, not merely somewhere inside the string", () => {
    // Both anchors matter independently: dropping `^` lets a match start
    // partway through leading garbage; dropping `$` lets one stop partway
    // through trailing garbage. Neither is a number.
    expect(validateSection("AuthConfig", { webhook_idp_timeout_s: "abc5.0" })[0]?.type).toBe("float_parsing");
    expect(validateSection("AuthConfig", { webhook_idp_timeout_s: "5.0abc" })[0]?.type).toBe("float_parsing");
  });

  it("accepts every shape of float the grammar names", () => {
    // Each of these pins one alternative in the float regex: a signed integer
    // with no fractional part, a trailing bare dot, a leading bare dot, a
    // multi-digit whole part, and an unsigned exponent with more than one
    // digit. A regex that quietly narrowed any one of these still passes a
    // suite that only ever wrote "5.0".
    const cases: Array<[string, number]> = [
      ["+5.0", 5],
      ["5", 5],
      ["5.", 5],
      [".5", 0.5],
      [".55", 0.55],
      ["12.5", 12.5],
      ["5e12", 5e12],
      ["5e+12", 5e12],
    ];
    for (const [text, value] of cases) {
      expect(validateSection("AuthConfig", { webhook_idp_timeout_s: text })).toEqual([]);
      expect(coerceSection("AuthConfig", { webhook_idp_timeout_s: text }).webhook_idp_timeout_s).toBe(value);
    }
  });

  it("falls back to its own default path, not app_path's", () => {
    // Both `app_path` and `assets_path` route through the same `cleanPath`
    // helper with their own fallback string; a test that only ever emptied
    // `app_path` could not tell "assets_path's fallback broke" apart from
    // "cleanPath's fallback parameter broke".
    expect(coerceSection("UiConfig", { assets_path: "" }).assets_path).toBe("/_terminal");
  });

  it("keeps the coerced value of a field that passed, not an empty stand-in", () => {
    // `coerceValue`'s datetime branch returns `{ value }` on success; a
    // mutant that returned `{}` instead would still be "not an error" and
    // would only show up in the coerced value itself.
    expect(coerceSection("SessionDefinition", { session_id: "s", created_at: "2020-01-01T00:00:00Z" }).created_at).toBe(
      "2020-01-01T00:00:00Z",
    );
    expect(coerceSection("SessionDefinition", { session_id: "s", created_at: 5 }).created_at).toBe(5);
  });
});

describe("what a whole document accepts", () => {
  it.each(golden.top_level_cases)("$name", (record) => {
    expect(validateServerConfig(record.kwargs)).toEqual(record.errors ?? []);
  });

  it("says which section a bad value is in", () => {
    expect(validateServerConfig({ auth: { identity_provider: "ldap" } })).toEqual([
      { type: "literal_error", loc: ["auth", "identity_provider"], msg: "Input should be 'local' or 'webhook'" },
    ]);
  });

  it("says which entry of a list a bad value is in", () => {
    expect(validateServerConfig({ graphical_targets: [{ width: "wide" }] })[0]?.loc).toEqual([
      "graphical_targets",
      0,
      "width",
    ]);
  });

  it("refuses a section given something that is not a table", () => {
    expect(validateServerConfig({ auth: "jwt" })[0]?.type).toBe("model_type");
  });

  it("accepts a document that says nothing", () => {
    // Every field has a default, so an empty file is a valid configuration —
    // and the defaults are the secure ones.
    expect(validateServerConfig({})).toEqual([]);
  });
});

/** The one complaint a document made of a single bad key produces. */
function complaint(field: string, value: number): string | undefined {
  return validateServerConfig({ [field]: value })[0]?.msg;
}

describe("the REST hijack ceilings", () => {
  // The corpus covers every value a JSON file can spell. These are the three
  // it cannot — `json.dumps` writes them as bare `Infinity`/`NaN`, which
  // `JSON.parse` refuses — so the corpus deliberately omits them and each port
  // carries them itself. TOML *can* spell all three (`x = inf`, `x = nan`), so
  // a deployment can reach them and the refusal has to be here.
  it("refuses a rate that is not finite, whichever way it is not", () => {
    // `inf` is the dangerous one: it passes every `>=` bound, so accepting it
    // would silently mean no limit at all — the fail-open that makes a trusted
    // limit worse than none. `-inf` and NaN go with it; none of the three is a
    // rate anybody meant to write. They are named in the interpreter's own
    // spelling rather than JavaScript's `Infinity`.
    for (const field of ["rest_acquire_rate_limit_per_sec", "rest_send_rate_limit_per_sec"]) {
      expect(complaint(field, Number.POSITIVE_INFINITY)).toBe(
        `Value error, ${field} must be a finite number >= 1.0, got: inf`,
      );
      expect(complaint(field, Number.NEGATIVE_INFINITY)).toBe(
        `Value error, ${field} must be a finite number >= 1.0, got: -inf`,
      );
      expect(complaint(field, Number.NaN)).toBe(`Value error, ${field} must be a finite number >= 1.0, got: nan`);
    }
  });

  it("still refuses a NaN if the finite check is ever removed", () => {
    // The bound is written `not value >= MIN` rather than `value < MIN` so a
    // NaN — false against every comparison — falls into the refusal instead of
    // sliding past a `<` test. With the finite check running first that form
    // is no longer load-bearing on its own, which is exactly why it is worth
    // pinning: it is the second line of defence, and the day someone deletes
    // the first one the NaN must still be refused.
    expect(validateServerConfig({ rest_send_rate_limit_per_sec: Number.NaN })).toHaveLength(1);
  });

  it("keeps the sign of a negative zero, which is a float and not an int", () => {
    // A negative zero is finite, so it reaches the bound rather than the
    // finite check. TOML can write `-0.0` and the interpreter's `repr` keeps
    // the sign, so the message names the value the operator wrote rather than
    // a different one that happens to compare equal to it.
    expect(complaint("rest_acquire_rate_limit_per_sec", -0)).toBe(
      "Value error, rest_acquire_rate_limit_per_sec must be >= 1.0, got: -0.0",
    );
  });
});

describe("the browser ceiling", () => {
  const FIELD = "browser_rate_limit_per_sec";

  it("refuses a zero, which reads like 'no limit' and means 'never'", () => {
    // The corpus has no browser refusal in it — the reference's change altered
    // only the validation of a field that already existed, so nothing was
    // re-recorded. These tests are the only thing pinning the behaviour here.
    expect(complaint(FIELD, 0)).toBe(`Value error, ${FIELD} must be >= 1.0, got: 0.0`);
  });

  it("refuses a negative", () => {
    expect(complaint(FIELD, -1)).toBe(`Value error, ${FIELD} must be >= 1.0, got: -1.0`);
  });

  it("refuses the whole band under the floor, not just zero", () => {
    // `0.5` is not "one message every two seconds", it is "never" — see the
    // bucket test below — so the whole `(0, 1)` band goes with the zero.
    expect(complaint(FIELD, 0.5)).toBe(`Value error, ${FIELD} must be >= 1.0, got: 0.5`);
    expect(complaint(FIELD, 0.99)).toBe(`Value error, ${FIELD} must be >= 1.0, got: 0.99`);
  });

  it("refuses a rate that is not finite, whichever way it is not", () => {
    expect(complaint(FIELD, Number.POSITIVE_INFINITY)).toBe(
      `Value error, ${FIELD} must be a finite number >= 1.0, got: inf`,
    );
    expect(complaint(FIELD, Number.NEGATIVE_INFINITY)).toBe(
      `Value error, ${FIELD} must be a finite number >= 1.0, got: -inf`,
    );
    expect(complaint(FIELD, Number.NaN)).toBe(`Value error, ${FIELD} must be a finite number >= 1.0, got: nan`);
  });

  it("accepts the floor itself, and the default that ships", () => {
    expect(validateServerConfig({ [FIELD]: 1.0 })).toEqual([]);
    expect(SERVER_CONFIG_DEFAULTS[FIELD]).toBe(300);
    expect(validateServerConfig({ [FIELD]: SERVER_CONFIG_DEFAULTS[FIELD] })).toEqual([]);
  });

  it("has the floor the bucket forces, measured rather than assumed", () => {
    // Why 1.0 and not a smaller number: `TokenBucket` defaults its burst to
    // one second of the rate, so a bucket under 1.0 can never hold the single
    // whole token a message costs. It denies the first call and keeps denying
    // however long the caller waits — the bucket is already full at its cap.
    let clock = 0;
    const bucket = (rate: number) => new TokenBucket(rate, { now: () => clock });
    for (const rate of [0, 0.5, 0.99]) {
      clock = 0;
      const denied = bucket(rate);
      expect(denied.allow()).toBe(false);
      clock = 3600;
      expect(denied.allow()).toBe(false);
    }
    for (const rate of [1.0, 300]) {
      clock = 0;
      expect(bucket(rate).allow()).toBe(true);
    }
  });
});
