//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  ApiKeyStore,
  type AuthSettings,
  applyCfAccessTeamDomain,
  buildWebhookSignature,
  canonicalTenantId,
  DEFAULT_ROLE,
  filterKnownRoles,
  INVALID_TENANT_MESSAGE,
  KNOWN_ROLES,
  rolesFromClaims,
  verifyWebhookSignature,
  WEBHOOK_MAX_AGE_S,
} from "./index.ts";

interface ServerAuthGolden {
  timestamp: string;
  now: number;
  secret: string;
  max_age_s: number;
  signatures: Array<{ name: string; secret: string; body: number[]; signature: string; verifies: boolean }>;
  verification: Record<string, boolean>;
  ambiguity: { signatures_collide: boolean; cross_verifies: boolean };
  roles: Array<{ name: string; input: unknown; resolved: string[] }>;
  role_failures: Array<{ name: string; error: string }>;
  known_roles: string[];
  default_role: string;
  tenants: Array<{ name: string; input: string | null; canonical: string | null }>;
  api_keys: Record<string, unknown>;
}

const golden = loadGolden<ServerAuthGolden>("serverauth_golden.json");
const keys = golden.api_keys;

const BODY = new TextEncoder().encode('{"decision":"allow"}');

/** The signature the reference produces for the recorded body. */
function goodSignature(): string {
  return buildWebhookSignature(golden.secret, BODY, golden.timestamp);
}

/** Verify with the recorded clock. */
function verify(
  secret: string | undefined,
  body: Uint8Array,
  signature: string | undefined,
  timestamp: string | undefined,
  now: number = golden.now,
): boolean {
  return verifyWebhookSignature(secret, body, signature, timestamp, { now });
}

describe("buildWebhookSignature", () => {
  it.each(golden.signatures)("$name", (record) => {
    expect(buildWebhookSignature(record.secret, Uint8Array.from(record.body), golden.timestamp)).toBe(record.signature);
  });

  it("signs over the timestamp as well as the body", () => {
    // Without the timestamp in the signed material a captured request can be
    // replayed for as long as the body stays valid.
    const body = Uint8Array.from(golden.signatures[0]?.body ?? []);
    expect(buildWebhookSignature(golden.secret, body, "1699999999")).not.toBe(
      buildWebhookSignature(golden.secret, body, golden.timestamp),
    );
  });

  it("is ambiguous where the body could be part of the timestamp", () => {
    // A recorded property of the shared scheme, not a decision of this port.
    // The signed material is timestamp + "." + body, so a body beginning with
    // digits and a dot can be re-read as part of the timestamp: these two
    // sign the same string, and because both timestamps name the same instant
    // a signature over one verifies for the other. Matching it deliberately —
    // the scheme is shared with the Go and C# ports, and changing it here
    // alone would break them.
    const one = buildWebhookSignature(golden.secret, new TextEncoder().encode("0.body"), "17000000");
    const two = buildWebhookSignature(golden.secret, new TextEncoder().encode("body"), "17000000.0");
    expect(one === two).toBe(golden.ambiguity.signatures_collide);
    expect(
      verifyWebhookSignature(golden.secret, new TextEncoder().encode("body"), one, "17000000.0", { now: 17000000 }),
    ).toBe(golden.ambiguity.cross_verifies);
  });

  it("depends on the secret", () => {
    const same = golden.signatures.find((entry) => entry.name === "a json body");
    const other = golden.signatures.find((entry) => entry.name === "a different secret");
    expect(other?.signature).not.toBe(same?.signature);
  });

  it("labels the algorithm", () => {
    expect(golden.signatures[0]?.signature.startsWith("sha256=")).toBe(true);
  });
});

describe("verifyWebhookSignature", () => {
  const cases: Record<string, () => boolean> = {
    "accepts a good signature": () => verify(golden.secret, BODY, goodSignature(), golden.timestamp),
    "accepts it without the prefix": () => verify(golden.secret, BODY, goodSignature().split("=")[1], golden.timestamp),
    "accepts a mixed-case prefix": () =>
      verify(golden.secret, BODY, `SHA256=${goodSignature().split("=")[1]}`, golden.timestamp),
    "accepts a padded signature": () => verify(golden.secret, BODY, `  ${goodSignature()}  `, golden.timestamp),
    "refuses no secret": () => verify(undefined, BODY, goodSignature(), golden.timestamp),
    "refuses an empty secret": () => verify("", BODY, goodSignature(), golden.timestamp),
    "refuses a whitespace secret": () => verify("   ", BODY, goodSignature(), golden.timestamp),
    "refuses no signature": () => verify(golden.secret, BODY, undefined, golden.timestamp),
    "refuses an empty signature": () => verify(golden.secret, BODY, "", golden.timestamp),
    "refuses a bare prefix": () => verify(golden.secret, BODY, "sha256=", golden.timestamp),
    "refuses no timestamp": () => verify(golden.secret, BODY, goodSignature(), undefined),
    "refuses a timestamp that is not a number": () => verify(golden.secret, BODY, goodSignature(), "soon"),
    "refuses a whitespace timestamp": () => verify(golden.secret, BODY, goodSignature(), "   "),
    "refuses a changed body": () =>
      verify(golden.secret, new TextEncoder().encode('{"decision":"deny"}'), goodSignature(), golden.timestamp),
    "refuses a wrong secret": () => verify("other", BODY, goodSignature(), golden.timestamp),
    "refuses a stale timestamp": () =>
      verify(golden.secret, BODY, goodSignature(), golden.timestamp, golden.now + golden.max_age_s + 1),
    "refuses a timestamp from the future": () =>
      verify(golden.secret, BODY, goodSignature(), golden.timestamp, golden.now - golden.max_age_s - 1),
    "accepts one at the edge of the window": () =>
      verify(golden.secret, BODY, goodSignature(), golden.timestamp, golden.now + golden.max_age_s),
    "refuses a signature made with a whitespace secret": () =>
      verify("   ", BODY, buildWebhookSignature("   ", BODY, golden.timestamp), golden.timestamp),
    "refuses a signature made with a timestamp that is not a number": () =>
      verify(golden.secret, BODY, buildWebhookSignature(golden.secret, BODY, "soon"), "soon"),
    "refuses a padded timestamp header": () => verify(golden.secret, BODY, goodSignature(), ` ${golden.timestamp} `),
    "refuses a short signature": () => verify(golden.secret, BODY, "sha256=abc", golden.timestamp),
    "refuses a signature for another timestamp": () =>
      verify(golden.secret, BODY, buildWebhookSignature(golden.secret, BODY, "1699999999"), golden.timestamp),
  };

  it.each(Object.entries(golden.verification))("%s", (name, expected) => {
    // Every one of these is a way in if it answered the other way.
    expect((cases[name] as () => boolean)()).toBe(expected);
  });

  it("fails closed with no signing key at all", () => {
    // An empty key HMACs to something anyone who knows the body and the
    // timestamp can forge, so the refusal has to come before the comparison.
    expect(golden.verification["refuses no secret"]).toBe(false);
    expect(golden.verification["refuses an empty secret"]).toBe(false);
    expect(golden.verification["refuses a whitespace secret"]).toBe(false);
    // The signature that an empty key would produce must not be accepted by
    // an empty key either.
    expect(verify("", BODY, buildWebhookSignature("", BODY, golden.timestamp), golden.timestamp)).toBe(false);
  });

  it("uses the real clock when given none", async () => {
    // Every other case injects one. A verifier that read the clock wrongly
    // would reject every live request while the whole suite stayed green.
    const stamp = String(Math.floor(Date.now() / 1000));
    const signature = buildWebhookSignature(golden.secret, BODY, stamp);
    expect(verifyWebhookSignature(golden.secret, BODY, signature, stamp)).toBe(true);
  });

  it("takes a window from the caller", () => {
    // A deployment with strict clocks can narrow it; one behind a slow proxy
    // can widen it.
    const stamp = golden.timestamp;
    const signature = buildWebhookSignature(golden.secret, BODY, stamp);
    expect(verifyWebhookSignature(golden.secret, BODY, signature, stamp, { now: golden.now + 10, maxAgeS: 5 })).toBe(
      false,
    );
    expect(verifyWebhookSignature(golden.secret, BODY, signature, stamp, { now: golden.now + 10, maxAgeS: 60 })).toBe(
      true,
    );
  });

  it("uses the recorded freshness window", () => {
    expect(WEBHOOK_MAX_AGE_S).toBe(golden.max_age_s);
  });

  it("bounds the window in both directions", () => {
    // A clock ahead of the sender is as much a replay window as one behind.
    expect(golden.verification["refuses a stale timestamp"]).toBe(false);
    expect(golden.verification["refuses a timestamp from the future"]).toBe(false);
  });

  it("verifies against the timestamp header, not the one inside the signature", () => {
    expect(golden.verification["refuses a signature for another timestamp"]).toBe(false);
  });

  it("covers every recorded case", () => {
    // A case added to the corpus and not here would pass by not running.
    expect(Object.keys(cases).sort()).toStrictEqual(Object.keys(golden.verification).sort());
  });
});

describe("filterKnownRoles", () => {
  it.each(golden.roles)("$name", (record) => {
    expect([...filterKnownRoles(record.input)].sort()).toStrictEqual(record.resolved);
  });

  it("drops a role nobody defined", () => {
    // Roles come from a JWT, a proxy header or a webhook IDP, none of which
    // this server controls. A compromised issuer must not be able to mint one.
    const record = golden.roles.find((entry) => entry.name === "an unknown role");
    expect(record?.resolved).toStrictEqual([DEFAULT_ROLE]);
  });

  it("keeps the known ones when they arrive alongside an unknown one", () => {
    const record = golden.roles.find((entry) => entry.name === "a known and an unknown role");
    expect(record?.resolved).toStrictEqual(["operator"]);
  });

  it("falls back to the least privileged role, not to nothing", () => {
    // An empty set is the kind of thing a caller reads as "no restrictions".
    expect(golden.default_role).toBe("viewer");
    expect(filterKnownRoles([]).size).toBe(1);
  });

  it("normalises case and whitespace", () => {
    // An issuer that sends "Admin" means admin; one that sends " operator "
    // means operator.
    expect([...filterKnownRoles(["ADMIN"])]).toStrictEqual(["admin"]);
    expect([...filterKnownRoles(["  operator "])]).toStrictEqual(["operator"]);
  });

  it("does not treat a bare string as a list of roles", () => {
    // "operator" iterates as characters, none of which is a role.
    const record = golden.roles.find((entry) => entry.name === "a string rather than a list");
    expect(record?.resolved).toStrictEqual([DEFAULT_ROLE]);
  });

  it("survives entries that are not strings", () => {
    const record = golden.roles.find((entry) => entry.name === "something that is not a string");
    expect(record?.resolved).toStrictEqual([DEFAULT_ROLE]);
  });

  it("reads a mapping's keys", () => {
    // A JWT claim may arrive as an object rather than a list.
    const record = golden.roles.find((entry) => entry.name === "a mapping, which iterates its keys");
    expect(record?.resolved).toStrictEqual(["operator"]);
    expect([...filterKnownRoles({ operator: 1, root: 2 })]).toStrictEqual(["operator"]);
  });

  it("reads any other collection's values", () => {
    expect([...filterKnownRoles(new Set(["viewer"]))]).toStrictEqual(["viewer"]);
    expect([...filterKnownRoles(["admin"])]).toStrictEqual(["admin"]);
  });

  it("refuses a claim that is not a collection at all", () => {
    // The reference raises here. Quietly returning the default role would
    // hide the caller's bug and grant access on a type error.
    expect(golden.role_failures.every((entry) => entry.error === "TypeError")).toBe(true);
    for (const value of [42, null, undefined, true]) {
      expect(() => filterKnownRoles(value)).toThrow(TypeError);
    }
  });

  it("knows exactly three roles", () => {
    expect([...KNOWN_ROLES].sort()).toStrictEqual(golden.known_roles);
    expect(golden.known_roles).toStrictEqual(["admin", "operator", "viewer"]);
  });
});

describe("rolesFromClaims", () => {
  function settings(jwt_default_role: string | null): AuthSettings {
    return {
      jwt_public_key_pem: null,
      jwt_algorithms: ["HS256"],
      jwt_issuer: "provide-uterm",
      jwt_audience: "provide-uterm-server",
      jwt_roles_claim: "roles",
      jwt_scopes_claim: "scope",
      jwt_tenant_claim: "tenant_id",
      clock_skew_seconds: 15,
      jwt_default_role,
    };
  }

  it("applies the configured default role when the claim is missing", () => {
    // Typical Cloudflare Access JWTs carry no roles claim at all — the gap
    // this default exists for. Go, C# and Python already have this fallback.
    expect([...rolesFromClaims({}, settings("operator"))]).toStrictEqual(["operator"]);
  });

  it("applies the configured default role when the claim has only unknown roles", () => {
    expect([...rolesFromClaims({ roles: ["superuser"] }, settings("operator"))]).toStrictEqual(["operator"]);
  });

  it("prefers a known claim role over the configured default", () => {
    expect([...rolesFromClaims({ roles: ["admin"] }, settings("operator"))]).toStrictEqual(["admin"]);
  });

  it("falls back to viewer when the configured default is not itself a known role", () => {
    expect([...rolesFromClaims({}, settings("superuser"))]).toStrictEqual([DEFAULT_ROLE]);
  });

  it("falls back to viewer when no default role is configured", () => {
    expect([...rolesFromClaims({}, settings(null))]).toStrictEqual([DEFAULT_ROLE]);
  });
});

describe("applyCfAccessTeamDomain", () => {
  // Mirrors Go's TestCfAccessTeamDomainAutoFill, C#'s
  // Load_FromToml_BindsCfAccessTeamDomainAndAppliesItsFill, and Python's
  // test_cf_access_team_domain_* tests in test_config_schema.py.

  it("fills omitted jwt_jwks_url and jwt_issuer", () => {
    const auth: Record<string, unknown> = { cf_access_team_domain: "myteam" };
    applyCfAccessTeamDomain(auth);
    expect(auth.jwt_jwks_url).toBe("https://myteam.cloudflareaccess.com/cdn-cgi/access/certs");
    expect(auth.jwt_issuer).toBe("https://myteam.cloudflareaccess.com");
  });

  it("does not override explicit values", () => {
    const auth: Record<string, unknown> = {
      cf_access_team_domain: "myteam",
      jwt_issuer: "https://custom.example",
      jwt_jwks_url: "https://custom.example/jwks",
    };
    applyCfAccessTeamDomain(auth);
    expect(auth.jwt_issuer).toBe("https://custom.example");
    expect(auth.jwt_jwks_url).toBe("https://custom.example/jwks");
  });

  it("strips scheme and path from the team domain", () => {
    const auth: Record<string, unknown> = {
      cf_access_team_domain: "https://other.cloudflareaccess.com/",
      jwt_issuer: "",
    };
    applyCfAccessTeamDomain(auth);
    expect(auth.jwt_issuer).toBe("https://other.cloudflareaccess.com");
  });

  it("does not synthesize endpoints when normalization removes the whole team", () => {
    const auth: Record<string, unknown> = {
      cf_access_team_domain: "https://.cloudflareaccess.com/",
      jwt_issuer: "",
    };
    applyCfAccessTeamDomain(auth);
    expect(auth).toStrictEqual({
      cf_access_team_domain: "https://.cloudflareaccess.com/",
      jwt_issuer: "",
    });
  });

  it("does nothing when no team domain is configured", () => {
    const auth: Record<string, unknown> = { jwt_issuer: "provide-uterm" };
    applyCfAccessTeamDomain(auth);
    expect(auth).toStrictEqual({ jwt_issuer: "provide-uterm" });
  });
});

describe("canonicalTenantId", () => {
  it.each(golden.tenants)("$name", (record) => {
    expect(canonicalTenantId(record.input ?? undefined) ?? null).toBe(record.canonical);
  });

  it("refuses a slug that does not start with an alphanumeric", () => {
    // The shape is shared verbatim with the Go and C# ports, so the same
    // tenant has to validate identically on every surface.
    for (const name of ["starting with a separator", "starting with a dot"]) {
      expect(golden.tenants.find((entry) => entry.name === name)?.canonical).toBeNull();
    }
  });

  it("refuses anything with a separator that is not one of the three", () => {
    for (const name of ["with a slash", "with a space inside", "with a null byte", "unicode"]) {
      expect(golden.tenants.find((entry) => entry.name === name)?.canonical).toBeNull();
    }
  });

  it("bounds the length at 128", () => {
    expect(golden.tenants.find((entry) => entry.name === "at the length limit")?.canonical).not.toBeNull();
    expect(golden.tenants.find((entry) => entry.name === "one past the limit")?.canonical).toBeNull();
  });

  it("treats empty and absent alike", () => {
    for (const name of ["empty", "whitespace only", "none"]) {
      expect(golden.tenants.find((entry) => entry.name === name)?.canonical).toBeNull();
    }
  });
});

describe("ApiKeyStore", () => {
  it("never stores the raw key", () => {
    // The digest is what is kept; a store that held the key would hand every
    // key over with one read.
    const store = new ApiKeyStore();
    const [raw, record] = store.create("ci");
    expect(raw.length).toBe(keys.raw_key_length);
    expect(record.keyHash).not.toBe(raw);
    expect(record.keyHash).not.toContain(raw);
    expect(record.keyHash).toHaveLength(keys.hash_length as number);
  });

  it("identifies a key by a prefix of its digest", () => {
    const store = new ApiKeyStore();
    const [, record] = store.create("ci");
    expect(record.keyId).toHaveLength(keys.key_id_length as number);
    expect(record.keyHash.startsWith(record.keyId)).toBe(keys.key_id_is_hash_prefix);
  });

  it("mints a different key every time", () => {
    const store = new ApiKeyStore();
    const [first] = store.create("one");
    const [second] = store.create("two");
    expect(first).not.toBe(second);
  });

  it("validates a key it minted", () => {
    const store = new ApiKeyStore();
    const [raw] = store.create("ci");
    expect(store.validate(raw)).toBeDefined();
  });

  it("records when a key was last used", () => {
    // The trail an operator uses to find a key nobody needs any more.
    const store = new ApiKeyStore();
    const [raw] = store.create("ci");
    expect(store.validate(raw)?.lastUsedAt).toBeDefined();
  });

  it("refuses a key it never minted", () => {
    expect(new ApiKeyStore().validate("not-a-key")).toBeUndefined();
  });

  it("refuses a key that is not the one it holds", () => {
    // The empty store never reaches the comparison; this one does, and a
    // comparison that matched anything would accept every guess.
    const store = new ApiKeyStore();
    store.create("ci");
    expect(store.validate("not-the-key")).toBeUndefined();
  });

  it("refuses a revoked key", () => {
    const store = new ApiKeyStore();
    const [raw, record] = store.create("dead");
    expect(store.revoke(record.keyId)).toBe(keys.revoke_reports_found);
    expect(store.validate(raw)).toBeUndefined();
  });

  it("reports revoking a key it does not have", () => {
    expect(new ApiKeyStore().revoke("nope")).toBe(keys.revoke_reports_unknown);
  });

  it("refuses an expired key", () => {
    // Expiry that is not enforced is a note in a database.
    const store = new ApiKeyStore();
    const [raw] = store.create("expired", { expiresInS: -1 });
    expect(store.validate(raw)).toBeUndefined();
  });

  it("accepts a key that has not expired yet", () => {
    const store = new ApiKeyStore();
    const [raw] = store.create("living", { expiresInS: 3600 });
    expect(store.validate(raw)).toBeDefined();
  });

  it("gives a key no expiry unless asked", () => {
    const store = new ApiKeyStore();
    const [, record] = store.create("ci");
    expect(record.expiresAt).toBeUndefined();
  });

  it("keeps the scopes it was given", () => {
    const store = new ApiKeyStore();
    const [, record] = store.create("ci", { scopes: ["operator"] });
    expect([...record.scopes]).toStrictEqual(keys.scopes_kept);
    const [, bare] = store.create("laptop");
    expect([...bare.scopes]).toStrictEqual(keys.default_scopes_empty);
  });

  it("leaves a flat key with no tenant", () => {
    const store = new ApiKeyStore();
    const [, record] = store.create("ci");
    expect(record.tenantId).toBe(keys.default_tenant_is_empty);
  });
});

describe("tenant-scoped keys", () => {
  /** A store holding one key for each of two tenants. */
  function twoTenants() {
    const store = new ApiKeyStore();
    const [, acme] = store.createForTenant("acme", "ci");
    const [, globex] = store.createForTenant("  globex  ", "ci");
    return { store, acme, globex };
  }

  it("stores the canonical tenant id", () => {
    const { acme, globex } = twoTenants();
    expect(acme.tenantId).toBe(keys.tenant_is_canonical);
    expect(globex.tenantId).toBe(keys.padded_tenant_is_trimmed);
  });

  it("refuses to mint a key for an invalid tenant", () => {
    // A key with a malformed tenant belongs to nobody, and every
    // tenant-scoped lookup would miss it.
    const store = new ApiKeyStore();
    expect(() => store.createForTenant("-bad", "ci")).toThrow(keys.invalid_tenant_refused as string);
    expect(() => store.createForTenant("", "ci")).toThrow(INVALID_TENANT_MESSAGE);
  });

  it("lists only its own tenant's keys", () => {
    const { store } = twoTenants();
    expect(store.listKeysForTenant("acme").map((key) => key.name)).toStrictEqual(keys.lists_only_its_own);
    // Two tenants, one key each: the full listing sees both, the per-tenant
    // one sees only its own.
    expect(store.listKeys()).toHaveLength(2);
  });

  it("lists nothing for a tenant that is invalid or unknown", () => {
    const { store } = twoTenants();
    expect(store.listKeysForTenant("-bad")).toStrictEqual([]);
    expect(store.listKeysForTenant("nobody")).toStrictEqual([]);
  });

  it("does not let one tenant revoke another's key", () => {
    // The whole point of the tenant field.
    const { store, acme } = twoTenants();
    expect(store.revokeForTenant(acme.keyId, "globex")).toBe(keys.revokes_only_its_own);
    expect(store.listKeysForTenant("acme")).toHaveLength(1);
  });

  it("revokes its own key", () => {
    const { store, acme } = twoTenants();
    expect(store.revokeForTenant(acme.keyId, "acme")).toBe(keys.revokes_its_own);
    expect(store.listKeysForTenant("acme").map((key) => key.name)).toStrictEqual(keys.revoked_key_leaves_the_listing);
  });

  it("does not let an invalid tenant reach a flat key", () => {
    // A flat key's tenant is the empty string. Treating an unusable tenant id
    // as "the empty tenant" would hand every legacy key to anyone who asked
    // with a malformed one.
    const store = new ApiKeyStore();
    const [, flat] = store.create("flat");
    expect(store.revokeForTenant(flat.keyId, "-bad")).toBe(keys.invalid_tenant_cannot_revoke_a_flat_key);
    expect(store.revokeForTenant(flat.keyId, "")).toBe(keys.empty_tenant_cannot_revoke_a_flat_key);
    expect(store.listKeys()[0]?.revoked).toBe(false);
  });

  it("refuses a revoke for an invalid tenant or an unknown key", () => {
    const { store, acme } = twoTenants();
    expect(store.revokeForTenant(acme.keyId, "-bad")).toBe(keys.revoke_for_an_invalid_tenant);
    expect(store.revokeForTenant("nope", "acme")).toBe(keys.revoke_an_unknown_key);
  });

  it("keeps a revoked key in the full listing", () => {
    // The listing is a record; a revoked key that vanished would take its own
    // audit trail with it.
    const { store, acme } = twoTenants();
    store.revokeForTenant(acme.keyId, "acme");
    expect(store.listKeys()).toHaveLength(2);
  });
});
