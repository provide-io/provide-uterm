//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  cloneTarget,
  GRAPHICAL_TARGET_ERROR_CODES,
  type GraphicalTarget,
  GraphicalTargetError,
  type GraphicalTargetScope,
  InMemoryGraphicalTargetRegistry,
  makeGraphicalTarget,
  PROTOCOL_LITEVIRT,
  PROTOCOL_MEMORY,
  PROTOCOL_RFB,
  parseLitevirtEndpoint,
  parseRfbEndpoint,
  publicCopy,
  SUPPORTED_PROTOCOLS,
  scopeForTenant,
  scopeIsValid,
  scopePermits,
  systemScope,
  toWireDict,
  validateTarget,
  wireTimestamp,
} from "./index.ts";

interface Outcome {
  value?: unknown;
  error?: string;
  message?: string;
  /** Set where the reference crashed instead of refusing. See below. */
  crash?: string;
}

interface Step extends Outcome {
  op: string;
  scope?: string;
  target_id?: string;
  fields?: Record<string, unknown>;
}

interface GraphicalGolden {
  protocols: string[];
  error_codes: string[];
  rfb: Array<Outcome & { name: string; endpoint: string | null }>;
  litevirt: Array<Outcome & { name: string; endpoint: string | null }>;
  definitions: Array<
    Outcome & {
      name: string;
      fields: Record<string, unknown>;
      protocol?: string;
      endpoint?: string | null;
      wire?: Record<string, unknown>;
    }
  >;
  scopes: Array<{
    name: string;
    scope_tenant: string | null;
    is_system: boolean;
    target_tenant: string | null;
    is_valid: boolean;
    permits: boolean;
  }>;
  scope_for_tenant: Array<{ tenant: string; scope: { tenant_id: string; is_system: boolean } | null; ok: boolean }>;
  system_scope: { tenant_id: string | null; is_system: boolean };
  public_copy: { value: Record<string, unknown> };
  scenarios: Array<{ name: string; steps: Step[] }>;
}

const golden = loadGolden<GraphicalGolden>("graphicaltargets_golden.json");

/** The instant the corpus was recorded at. */
const WHEN = new Date("2026-01-02T03:04:05+00:00");

/** The corpus's snake_case fields as a target. */
function targetFrom(fields: Record<string, unknown>): GraphicalTarget {
  const updatedAt = fields.updated_at;
  return makeGraphicalTarget({
    createdAt: WHEN,
    ...(fields.target_id === undefined ? {} : { targetId: fields.target_id as string }),
    ...(fields.tenant_id === undefined ? {} : { tenantId: fields.tenant_id as string }),
    ...(fields.display_name === undefined ? {} : { displayName: fields.display_name as string }),
    ...(fields.protocol === undefined ? {} : { protocol: fields.protocol as string }),
    ...(fields.endpoint === undefined ? {} : { endpoint: fields.endpoint as string | null }),
    ...(fields.secret === undefined ? {} : { secret: fields.secret as string | null }),
    ...(fields.width === undefined ? {} : { width: fields.width as number }),
    ...(fields.height === undefined ? {} : { height: fields.height as number }),
    ...(fields.is_system === undefined ? {} : { isSystem: fields.is_system as boolean }),
    ...(fields.is_static === undefined ? {} : { isStatic: fields.is_static as boolean }),
    ...(fields.ca_secret_ref === undefined ? {} : { caSecretRef: fields.ca_secret_ref as string | null }),
    ...(fields.client_cert_secret_ref === undefined
      ? {}
      : { clientCertSecretRef: fields.client_cert_secret_ref as string | null }),
    ...(fields.client_key_secret_ref === undefined
      ? {}
      : { clientKeySecretRef: fields.client_key_secret_ref as string | null }),
    ...(fields.created_by === undefined ? {} : { createdBy: fields.created_by as string | null }),
    ...(fields.updated_by === undefined ? {} : { updatedBy: fields.updated_by as string | null }),
    ...(updatedAt === undefined ? {} : { updatedAt: new Date(updatedAt as string) }),
    ...(fields.config === undefined ? {} : { config: fields.config as Record<string, unknown> }),
  });
}

/** What a call did: its value, or the code and message it refused with. */
function outcomeOf(call: () => unknown): Outcome {
  try {
    return { value: call() ?? null };
  } catch (error) {
    if (error instanceof GraphicalTargetError) {
      return { error: error.code, message: error.message };
    }
    throw error;
  }
}

/** Assert a call matched what the reference did, value or refusal. */
function expectOutcome(recorded: Outcome, call: () => unknown): void {
  const actual = outcomeOf(call);
  if (recorded.crash !== undefined) {
    // A recorded divergence. The reference guards only its port lookup, so an
    // address whose bracket is never closed escapes as a bare ValueError — a
    // 500 where the operator earned a 400. The port refuses with a code.
    expect(actual.error).toBe("INVALID");
    return;
  }
  if (recorded.error !== undefined) {
    expect(actual.error).toBe(recorded.error);
    expect(actual.message).toBe(recorded.message);
    return;
  }
  expect(actual.error).toBeUndefined();
  expect(actual.value).toEqual(recorded.value);
  // Key order too: the wire shape is what a client parses, and a listing that
  // reorders its keys is a different document to anything that signs it.
  if (recorded.value !== null && typeof recorded.value === "object" && !Array.isArray(recorded.value)) {
    expect(Object.keys(actual.value as object)).toEqual(Object.keys(recorded.value as object));
  }
}

describe("what a graphical target may say", () => {
  it("speaks the protocols the reference speaks", () => {
    expect([...SUPPORTED_PROTOCOLS].sort()).toEqual(golden.protocols);
    expect(SUPPORTED_PROTOCOLS.has(PROTOCOL_MEMORY)).toBe(true);
    expect(SUPPORTED_PROTOCOLS.has(PROTOCOL_RFB)).toBe(true);
    expect(SUPPORTED_PROTOCOLS.has(PROTOCOL_LITEVIRT)).toBe(true);
  });

  it("refuses with the codes the reference refuses with", () => {
    expect(Object.values(GRAPHICAL_TARGET_ERROR_CODES)).toEqual(golden.error_codes);
  });

  it.each(golden.definitions)("$name", (record) => {
    const target = targetFrom(record.fields);
    expectOutcome(record.error === undefined ? { value: record.wire } : record, () => {
      validateTarget(target);
      return toWireDict(target);
    });
    if (record.error === undefined) {
      // Validation normalises in place, as the reference does: what is stored
      // afterwards is the endpoint as it was read, not as it was typed.
      expect(target.protocol).toBe(record.protocol);
      expect(target.endpoint).toBe(record.endpoint ?? null);
    }
  });

  it("defaults to a target nobody owns, of a size a screen has", () => {
    const target = makeGraphicalTarget({ createdAt: WHEN });
    expect(target.protocol).toBe(PROTOCOL_RFB);
    expect(target.width).toBe(640);
    expect(target.height).toBe(480);
    expect(target.tenantId).toBe("");
    expect(target.endpoint).toBeNull();
    expect(target.isSystem).toBe(false);
    expect(target.config).toEqual({});
  });

  it("gives each target its own settings, not a shared one", () => {
    // Two targets sharing a config map is one target's parameters showing up
    // in another's session.
    const first = makeGraphicalTarget({ createdAt: WHEN });
    const second = makeGraphicalTarget({ createdAt: WHEN });
    first.config.depth = 24;
    expect(second.config).toEqual({});
  });

  it("copies the settings when it copies a target", () => {
    const target = makeGraphicalTarget({ createdAt: WHEN, config: { depth: 24 } });
    const copy = cloneTarget(target);
    copy.config.depth = 8;
    expect(target.config.depth).toBe(24);
    expect(copy.targetId).toBe(target.targetId);
  });

  it("holds nothing of the caller's map after it is built", () => {
    const config = { depth: 24 };
    const target = makeGraphicalTarget({ createdAt: WHEN, config });
    config.depth = 8;
    expect(target.config.depth).toBe(24);
  });
});

describe("what leaves the server", () => {
  it("strips every secret and keeps the settings", () => {
    const target = makeGraphicalTarget({
      targetId: "vm1",
      endpoint: "vm.example:5900",
      secret: "s3cret", // pragma: allowlist secret
      caSecretRef: "env:CA", // pragma: allowlist secret
      clientCertSecretRef: "env:CERT", // pragma: allowlist secret
      clientKeySecretRef: "env:KEY", // pragma: allowlist secret
      createdAt: WHEN,
      config: { vm_name: "guest-1" },
    });
    const wire = toWireDict(publicCopy(target));
    expect(wire).toEqual(golden.public_copy.value);
    expect(Object.keys(wire)).toEqual(Object.keys(golden.public_copy.value));
  });

  it("leaves the target it copied from alone", () => {
    // Stripping for a listing must not disarm the target the server connects
    // with.
    const target = makeGraphicalTarget({
      targetId: "vm1",
      secret: "s3cret", // pragma: allowlist secret
      caSecretRef: "env:CA", // pragma: allowlist secret
      createdAt: WHEN,
    });
    publicCopy(target);
    expect(target.secret).toBe("s3cret");
    expect(target.caSecretRef).toBe("env:CA");
  });

  it("omits what was never set rather than sending it empty", () => {
    const wire = toWireDict(makeGraphicalTarget({ targetId: "vm1", createdAt: WHEN }));
    for (const key of [
      "endpoint",
      "secret",
      "ca_secret_ref",
      "client_cert_secret_ref",
      "client_key_secret_ref",
      "created_by",
      "updated_by",
      "updated_at",
      "config",
    ]) {
      expect(key in wire).toBe(false);
    }
  });

  it("writes a time the way the reference writes one", () => {
    // Byte-for-byte, so a document signed on one runtime verifies on the
    // other.
    expect(wireTimestamp(new Date("2026-01-02T03:04:05Z"))).toBe("2026-01-02T03:04:05+00:00");
    expect(wireTimestamp(new Date("2026-01-02T03:04:05.123Z"))).toBe("2026-01-02T03:04:05.123000+00:00");
    expect(wireTimestamp(new Date("2026-12-31T23:59:59.999Z"))).toBe("2026-12-31T23:59:59.999000+00:00");
    expect(wireTimestamp(new Date("0999-01-02T03:04:05Z"))).toBe("0999-01-02T03:04:05+00:00");
  });
});

describe("where an rfb endpoint points", () => {
  it.each(golden.rfb)("$name", (record) => {
    expectOutcome(record, () => parseRfbEndpoint(record.endpoint));
  });

  it("takes the port as a number, not as the text of one", () => {
    expect(parseRfbEndpoint("vm.example:5900")).toEqual(["vm.example", 5900]);
  });

  it("lowercases the host, as the reference's parser does", () => {
    expect(parseRfbEndpoint("VM.Example:5900")[0]).toBe("vm.example");
  });

  it("refuses a port outside what a port can be", () => {
    for (const port of ["0", "65536", "99999", "-1", "abc", ""]) {
      expect(() => parseRfbEndpoint(`vm.example:${port}`)).toThrow(GraphicalTargetError);
    }
    expect(parseRfbEndpoint("vm.example:65535")[1]).toBe(65535);
    expect(parseRfbEndpoint("vm.example:1")[1]).toBe(1);
  });

  it("refuses an address whose bracket is never closed", () => {
    // A recorded divergence. The reference guards only its port lookup, so
    // this escapes as a bare ValueError and an operator's typo becomes a 500.
    // Everything a client can send has to come back coded.
    for (const endpoint of ["[2001:db8::1:5900", "[::1", "["]) {
      expect(() => parseRfbEndpoint(endpoint)).toThrow(GraphicalTargetError);
      expect(() => parseLitevirtEndpoint(endpoint)).toThrow(GraphicalTargetError);
    }
  });

  it("says which of the two things is wrong", () => {
    // An operator who typed no port and one who typed a bad one need
    // different corrections.
    expect(() => parseRfbEndpoint("vm.example")).toThrow("invalid endpoint; expected host:port or rfb://host:port");
    expect(() => parseRfbEndpoint("vm.example:abc")).toThrow("invalid endpoint port");
    expect(() => parseRfbEndpoint("")).toThrow("endpoint is required for protocol rfb");
  });
});

describe("where a litevirt endpoint points", () => {
  it.each(golden.litevirt)("$name", (record) => {
    expectOutcome(record, () => parseLitevirtEndpoint(record.endpoint));
  });

  it("takes no scheme, unlike rfb", () => {
    // A caller who pasted an rfb address here has named a different service.
    expect(() => parseLitevirtEndpoint("rfb://vm.example:9000")).toThrow("invalid endpoint port");
    expect(parseRfbEndpoint("rfb://vm.example:9000")).toEqual(["vm.example", 9000]);
  });

  it("says it is required by its own name", () => {
    expect(() => parseLitevirtEndpoint("")).toThrow("endpoint is required for protocol litevirt");
  });

  it("refuses a host with nothing before the port", () => {
    expect(() => parseLitevirtEndpoint(":9000")).toThrow("invalid endpoint; expected host:port");
  });
});

describe("who may see a target", () => {
  it.each(golden.scopes)("$name", (record) => {
    const scope: GraphicalTargetScope = { tenantId: record.scope_tenant, isSystem: record.is_system };
    expect(scopeIsValid(scope)).toBe(record.is_valid);
    expect(scopePermits(scope, record.target_tenant)).toBe(record.permits);
  });

  it.each(golden.scope_for_tenant)("a scope for $tenant", (record) => {
    const scope = scopeForTenant(record.tenant);
    expect(scope === null).toBe(!record.ok);
    if (scope !== null) {
      expect(scope.tenantId).toBe(record.scope?.tenant_id);
      expect(scope.isSystem).toBe(record.scope?.is_system);
    }
  });

  it("is exactly one of the two, never both and never neither", () => {
    // A scope that is both would let one tenant's session reach the system's
    // consoles; one that is neither is an unauthenticated caller.
    expect(scopeIsValid({ tenantId: "acme", isSystem: true })).toBe(false);
    expect(scopeIsValid({ tenantId: null, isSystem: false })).toBe(false);
    expect(scopeIsValid({ tenantId: "acme", isSystem: false })).toBe(true);
    expect(scopeIsValid({ tenantId: null, isSystem: true })).toBe(true);
  });

  it("permits nothing at all when it is neither", () => {
    for (const tenant of ["acme", "", null]) {
      expect(scopePermits({ tenantId: null, isSystem: false }, tenant)).toBe(false);
      expect(scopePermits({ tenantId: "acme", isSystem: true }, tenant)).toBe(false);
    }
  });

  it("lets a tenant reach only its own", () => {
    const scope = { tenantId: "acme", isSystem: false };
    expect(scopePermits(scope, "acme")).toBe(true);
    expect(scopePermits(scope, "other")).toBe(false);
    // Not a target nobody owns, either: an untenanted target is the system's.
    expect(scopePermits(scope, "")).toBe(false);
    expect(scopePermits(scope, null)).toBe(false);
  });

  it("takes a blank tenant as no scope rather than as an empty one", () => {
    // An empty tenant would otherwise match every untenanted target.
    expect(scopeForTenant("")).toBeNull();
    expect(scopeForTenant("   ")).toBeNull();
    expect(scopeForTenant("acme")).toEqual({ tenantId: "acme", isSystem: false });
  });

  it("keeps the tenant as it was written, spaces and all", () => {
    // Only the emptiness test trims; a scope that trimmed would match a
    // tenant whose id is not the one presented.
    expect(scopeForTenant("  acme  ")).toEqual({ tenantId: "  acme  ", isSystem: false });
  });

  it("names the system scope as the reference names it", () => {
    expect(systemScope().tenantId).toBe(golden.system_scope.tenant_id);
    expect(systemScope().isSystem).toBe(golden.system_scope.is_system);
    expect(scopePermits(systemScope(), "anybody")).toBe(true);
  });
});

/** A clock that ticks a second per reading, so the corpus's times are exact. */
function tickingClock(): () => Date {
  let ticks = 0;
  return () => {
    ticks += 1;
    return new Date(WHEN.getTime() + ticks * 1000);
  };
}

const SCENARIO_SCOPES: Record<string, GraphicalTargetScope> = {
  system: { tenantId: null, isSystem: true },
  acme: { tenantId: "acme", isSystem: false },
  other: { tenantId: "other", isSystem: false },
  broken: { tenantId: null, isSystem: false },
};

describe("the registry", () => {
  it.each(golden.scenarios)("$name", (scenario) => {
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    for (const step of scenario.steps) {
      const scope = step.scope === undefined ? SCENARIO_SCOPES.system : SCENARIO_SCOPES[step.scope];
      const target = step.fields === undefined ? undefined : targetFrom(step.fields);
      const operations: Record<string, () => unknown> = {
        create: () => toWireDict(registry.create(scope as GraphicalTargetScope, target as GraphicalTarget)),
        update: () => toWireDict(registry.update(scope as GraphicalTargetScope, target as GraphicalTarget)),
        delete: () => registry.delete(scope as GraphicalTargetScope, step.target_id as string),
        add_static: () => registry.addStatic(target as GraphicalTarget),
        close: () => registry.close(),
        get: () => {
          const found = registry.get(scope as GraphicalTargetScope, step.target_id as string);
          return found === null ? null : toWireDict(found);
        },
        list: () => registry.list(scope as GraphicalTargetScope).map(toWireDict),
      };
      expectOutcome(step, operations[step.op] as () => unknown);
    }
  });

  it("hands back a copy, not the target it holds", () => {
    // A caller that mutated what it was given would rewrite the registry
    // without going through the scope checks.
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    const scope = systemScope();
    registry.create(scope, makeGraphicalTarget({ targetId: "vm1", endpoint: "vm.example:5900", createdAt: WHEN }));
    const first = registry.get(scope, "vm1") as GraphicalTarget;
    first.endpoint = "evil.example:5900";
    first.config.depth = 8;
    const second = registry.get(scope, "vm1") as GraphicalTarget;
    expect(second.endpoint).toBe("vm.example:5900");
    expect(second.config).toEqual({});
  });

  it("keeps nothing of the target it was handed", () => {
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    const scope = systemScope();
    const target = makeGraphicalTarget({ targetId: "vm1", endpoint: "vm.example:5900", createdAt: WHEN });
    registry.create(scope, target);
    target.endpoint = "evil.example:5900";
    expect((registry.get(scope, "vm1") as GraphicalTarget).endpoint).toBe("vm.example:5900");
  });

  it("stamps the creation time itself", () => {
    // Not from the caller: a target claiming to predate the audit trail is a
    // target that was never approved.
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    const created = registry.create(
      systemScope(),
      makeGraphicalTarget({
        targetId: "vm1",
        endpoint: "vm.example:5900",
        createdAt: new Date("1999-01-01T00:00:00Z"),
      }),
    );
    expect(wireTimestamp(created.createdAt)).toBe("2026-01-02T03:04:06+00:00");
  });

  it("does not let an update rewrite who made a target or when", () => {
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    const scope = systemScope();
    registry.create(
      scope,
      makeGraphicalTarget({ targetId: "vm1", endpoint: "vm.example:5900", createdBy: "ada", createdAt: WHEN }),
    );
    const updated = registry.update(
      scope,
      makeGraphicalTarget({
        targetId: "vm1",
        endpoint: "vm.example:5901",
        createdBy: "mallory",
        createdAt: new Date("1999-01-01T00:00:00Z"),
      }),
    );
    expect(updated.createdBy).toBe("ada");
    expect(wireTimestamp(updated.createdAt)).toBe("2026-01-02T03:04:06+00:00");
    expect(wireTimestamp(updated.updatedAt as Date)).toBe("2026-01-02T03:04:07+00:00");
  });

  it("checks the scope before it checks anything else", () => {
    // So a caller learns nothing about a tenant it cannot reach — not even
    // whether an identifier is taken.
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    registry.create(
      { tenantId: "acme", isSystem: false },
      makeGraphicalTarget({ targetId: "vm1", tenantId: "acme", endpoint: "vm.example:5900", createdAt: WHEN }),
    );
    expect(() =>
      registry.create(
        { tenantId: "other", isSystem: false },
        makeGraphicalTarget({ targetId: "vm1", tenantId: "other", endpoint: "vm.example:5900", createdAt: WHEN }),
      ),
    ).toThrow("graphical target already exists");
    expect(registry.get({ tenantId: "other", isSystem: false }, "vm1")).toBeNull();
  });

  it("uses a clock of its own when it is given none", () => {
    const registry = new InMemoryGraphicalTargetRegistry();
    const before = Date.now();
    const created = registry.create(
      systemScope(),
      makeGraphicalTarget({ targetId: "vm1", endpoint: "vm.example:5900", createdAt: WHEN }),
    );
    expect(created.createdAt.getTime()).toBeGreaterThanOrEqual(before);
  });

  it("still seeds after it is closed, as the reference does", () => {
    // Recorded rather than corrected: seeding is not scope-gated in the
    // reference, so it is the one operation a closed registry still takes.
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    registry.close();
    registry.addStatic(makeGraphicalTarget({ targetId: "seed", endpoint: "seed.example:5900", createdAt: WHEN }));
    expect(() => registry.get(systemScope(), "seed")).toThrow("graphical target registry is closed");
  });

  it("closes once and stays closed", () => {
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    registry.close();
    registry.close();
    expect(() => registry.list(systemScope())).toThrow(GraphicalTargetError);
  });

  it("marks a seeded target as the system's", () => {
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    registry.addStatic(
      makeGraphicalTarget({ targetId: "seed", endpoint: "seed.example:5900", isSystem: false, createdAt: WHEN }),
    );
    expect((registry.get(systemScope(), "seed") as GraphicalTarget).isSystem).toBe(true);
  });

  it("returns nothing for a target that was never there", () => {
    const registry = new InMemoryGraphicalTargetRegistry(tickingClock());
    expect(registry.get(systemScope(), "absent")).toBeNull();
    expect(registry.list(systemScope())).toEqual([]);
  });
});
