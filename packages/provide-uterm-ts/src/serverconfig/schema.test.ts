//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type ConfigError,
  coerceSection,
  SECTION_FIELD_SPECS,
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
