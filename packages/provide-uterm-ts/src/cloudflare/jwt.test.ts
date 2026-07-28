//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import type { JwtConfig } from "./index.ts";
import {
  b64urlDecode,
  checkAudience,
  checkExp,
  checkIssuer,
  checkNbf,
  defaultConfig,
  extractBearerOrCookie,
  extractRoles,
  findJwk,
  JwtValidationError,
  parseJwtParts,
  parseRolesClaim,
  resolveRole,
  validateClaims,
} from "./index.ts";

interface Raised {
  error: string | null;
  message: string | null;
  result: unknown;
}

interface JwtGolden {
  now: number;
  default_clock_skew_seconds: number;
  default_roles_claim: string;
  default_scopes_claim: string;
  default_role: string;
  b64url: Array<{ name: string; encoded: string } & Raised>;
  parts: Array<{ name: string; token: string } & Raised>;
  signing_input: string;
  signature_bytes: number[];
  jwks: Array<{ name: string; jwks: Record<string, unknown>; kid: string | null; alg: string | null } & Raised>;
  claims: Array<{ name: string; payload: Record<string, unknown>; config: Record<string, unknown> } & Raised>;
  exp_alone: Raised;
  nbf_alone: Raised;
  issuer_alone: Raised;
  audience_alone: Raised;
  roles_claim: Array<{ name: string; raw: unknown; result: string[] }>;
  roles: Array<{ name: string; claims: Record<string, unknown>; config: Record<string, unknown>; result: string[] }>;
  resolve: Array<{ name: string; roles: string[]; result: string }>;
  bearer: Array<{ name: string; result: string | null }>;
}

const golden = loadGolden<JwtGolden>("cfjwtclaims_golden.json");
const NOW = golden.now;

/** The default configuration with a field or two changed. */
function config(overrides: Record<string, unknown> = {}): JwtConfig {
  return { ...defaultConfig().jwt, ...overrides } as JwtConfig;
}

/** What a call refuses, in the shape the corpus records. */
function raised(call: () => unknown): Raised {
  try {
    const result = call();
    return { error: null, message: null, result: result === undefined ? null : result };
  } catch (error) {
    // The reference raises `JSONDecodeError` where this engine raises
    // `SyntaxError`, and `binascii.Error` where it raises `Error`. The corpus
    // name is mapped rather than compared directly.
    const name = (error as Error).constructor.name;
    return {
      error:
        error instanceof JwtValidationError
          ? "JwtValidationError"
          : name === "SyntaxError"
            ? "JSONDecodeError"
            : "Error",
      message: (error as Error).message,
      result: null,
    };
  }
}

/**
 * A header bag that answers with exactly what it was given.
 *
 * Not a `Headers`, which strips surrounding whitespace on construction — the
 * reference reads a plain mapping, so the corpus records values a `Headers`
 * would already have trimmed. The runtime's own normalisation is asserted
 * separately below.
 */
function rawHeaders(values: Record<string, string>): { headers: { get(name: string): string | undefined } } {
  return { headers: { get: (name) => values[name] } };
}

/** A request carrying the corpus's headers, or not carrying them at all. */
const BEARER_REQUESTS: Record<string, unknown> = {
  "a bearer header": rawHeaders({ Authorization: "Bearer abc.def.ghi" }),
  "a lowercase scheme": rawHeaders({ Authorization: "bearer abc" }),
  "a shouted scheme": rawHeaders({ Authorization: "BEARER abc" }),
  "a padded token": rawHeaders({ Authorization: "Bearer   abc  " }),
  "a bearer with no token": rawHeaders({ Authorization: "Bearer " }),
  "another scheme": rawHeaders({ Authorization: "Basic abc" }),
  "a scheme that merely starts the same": rawHeaders({ Authorization: "Bearerabc" }),
  "no authorization at all": rawHeaders({}),
  "the access cookie": rawHeaders({ Cookie: "CF_Authorization=abc" }),
  "the cookie among others": rawHeaders({ Cookie: "a=1; CF_Authorization=abc; b=2" }),
  "a padded cookie": rawHeaders({ Cookie: " CF_Authorization = abc " }),
  "an empty cookie": rawHeaders({ Cookie: "CF_Authorization=" }),
  "a cookie of another name": rawHeaders({ Cookie: "Other=abc" }),
  "a cookie whose name merely contains it": rawHeaders({ Cookie: "XCF_Authorization=abc" }),
  "a cookie value containing an equals sign": rawHeaders({ Cookie: "CF_Authorization=a=b" }),
  "a bare cookie before the real one": rawHeaders({ Cookie: "CF_Authorizations; CF_Authorization=abc" }),
  both: rawHeaders({ Authorization: "Bearer hdr", Cookie: "CF_Authorization=cke" }),
  "a bearer with no token, and a cookie": rawHeaders({ Authorization: "Bearer ", Cookie: "CF_Authorization=cke" }),
  "headers that raise": {
    headers: {
      get(): string {
        throw new Error("headers unreadable");
      },
    },
  },
  "no headers at all": {},
};

describe("decoding a token segment", () => {
  it.each(golden.b64url)("$name", (record) => {
    const outcome = raised(() => [...b64urlDecode(record.encoded)]);
    expect(outcome.error).toBe(record.error);
    expect(outcome.result).toEqual(record.result);
  });

  it("reads base64url, not base64", () => {
    // The two alphabets differ in exactly two characters, and a token's
    // signature is full of them.
    expect([...b64urlDecode("-_--")]).toEqual([0xfb, 0xff, 0xbe]);
  });

  it("supplies the padding a token leaves off", () => {
    expect([...b64urlDecode("YWJjZGU")]).toEqual([...Buffer.from("abcde")]);
    expect([...b64urlDecode("YWJjZGVm")]).toEqual([...Buffer.from("abcdef")]);
  });

  it("discards what is not in the alphabet rather than refusing it", () => {
    // CPython's decoder does, which is why a header of `!!!` reaches the JSON
    // parser as empty rather than failing to decode. A strict decoder here
    // would report the wrong failure for a malformed token.
    expect([...b64urlDecode("!!!")]).toEqual([]);
    expect([...b64urlDecode("====")]).toEqual([]);
  });

  it("still refuses a length no padding could complete", () => {
    // Discarding shifts the padding arithmetic, which is computed from the
    // original length — so a segment with punctuation in the middle fails
    // where one made entirely of punctuation does not.
    expect(() => b64urlDecode("YWJ!jZA")).toThrow();
    expect(() => b64urlDecode("a")).toThrow();
    expect(() => b64urlDecode("YW Jj ZA")).toThrow();
  });
});

describe("splitting a token", () => {
  it.each(golden.parts)("$name", (record) => {
    const outcome = raised(() => {
      const parts = parseJwtParts(record.token);
      return [parts.header, parts.payload];
    });
    expect(outcome.error).toBe(record.error);
    expect(outcome.result).toEqual(record.result);
  });

  it("returns the bytes the signature covers", () => {
    // Everything before the last dot, as sent — re-encoding the header would
    // change the bytes and every signature would fail.
    const parts = parseJwtParts(`${golden.signing_input}.${Buffer.from([1, 2, 3]).toString("base64url")}`);
    expect(Buffer.from(parts.signingInput).toString()).toBe(golden.signing_input);
    expect([...parts.signature]).toEqual(golden.signature_bytes);
  });

  it("insists on exactly three parts", () => {
    for (const token of ["a.b", "a.b.c.d", "abc", ""]) {
      expect(() => parseJwtParts(token)).toThrow(/expected 3 parts/);
    }
  });

  it("does not check that a header is an object", () => {
    // Nothing downstream does either — a header that is a list reads its
    // fields as undefined and fails the algorithm check instead. Pinned
    // rather than assumed.
    const listHeader = Buffer.from("[1,2]").toString("base64url");
    const payload = Buffer.from('{"sub":"u1"}').toString("base64url");
    expect(parseJwtParts(`${listHeader}.${payload}.AQID`).header).toEqual([1, 2]);
  });
});

describe("choosing a key from a JWKS", () => {
  it.each(golden.jwks)("$name", (record) => {
    const outcome = raised(() => findJwk(record.jwks, record.kid ?? undefined, record.alg ?? undefined));
    expect(outcome.error).toBe(record.error);
    expect(outcome.result).toEqual(record.result);
  });

  it("prefers the key the token named", () => {
    // A key whose algorithm matches is not a substitute for the key the token
    // asked for; taking it would verify against a key the issuer had rotated
    // away from.
    const jwks = { keys: [{ alg: "RS256" }, { kid: "k1" }] };
    expect(findJwk(jwks, "k1", "RS256")).toEqual({ kid: "k1" });
  });

  it("refuses when the named key is absent", () => {
    expect(() => findJwk({ keys: [{ kid: "k0" }] }, "k9", "RS256")).toThrow(/no matching key/);
  });

  it("distinguishes an empty JWKS from a missing key", () => {
    // One is a misconfigured endpoint and the other a rotated key.
    expect(() => findJwk({ keys: [] }, "k1", "RS256")).toThrow(/JWKS contains no keys/);
    expect(() => findJwk({}, "k1", "RS256")).toThrow(/JWKS contains no keys/);
  });
});

describe("validating the standard claims", () => {
  it.each(golden.claims)("$name", (record) => {
    const outcome = raised(() => validateClaims(record.payload, config(record.config), NOW));
    expect(outcome.error).toBe(record.error);
    expect(outcome.message).toBe(record.message);
  });

  it("treats an expiry of null as missing", () => {
    // `payload.get("exp")` yields None either way in the reference, so a null
    // claim is an absent one — not a value to compare against the clock.
    expect(() => validateClaims({ exp: null }, config(), NOW)).toThrow(/missing exp claim/);
  });

  it("checks the expiry before the not-before", () => {
    // Both wrong at once: which failure is reported says which check ran
    // first, and a client told "not yet valid" would wait rather than
    // re-authenticate.
    expect(() => validateClaims({ nbf: NOW + 60 }, config(), NOW)).toThrow(/missing exp claim/);
  });

  it("truncates a fractional skew rather than tolerating it", () => {
    // `int()` truncates, so the fractional part is not tolerance the
    // deployment gets.
    const skewed = config({ clock_skew_seconds: 30.9 });
    expect(() => validateClaims({ exp: NOW - 30.5 }, skewed, NOW)).toThrow(/expired/);
    expect(() => validateClaims({ exp: NOW - 29.5 }, skewed, NOW)).not.toThrow();
  });

  it("refuses a token with no expiry", () => {
    // Not treated as non-expiring, which is what a missing claim would mean
    // if it were merely skipped.
    expect(() => validateClaims({}, config(), NOW)).toThrow(/missing exp claim/);
  });

  it("allows a token expiring exactly now", () => {
    expect(() => validateClaims({ exp: NOW }, config(), NOW)).not.toThrow();
  });

  it("allows skew in both directions", () => {
    const skew = golden.default_clock_skew_seconds;
    expect(() => validateClaims({ exp: NOW - skew }, config(), NOW)).not.toThrow();
    expect(() => validateClaims({ exp: NOW - skew - 1 }, config(), NOW)).toThrow(/expired/);
    expect(() => validateClaims({ exp: NOW + 60, nbf: NOW + skew }, config(), NOW)).not.toThrow();
    expect(() => validateClaims({ exp: NOW + 60, nbf: NOW + skew + 1 }, config(), NOW)).toThrow(/not yet valid/);
  });

  it("never lets a negative skew shorten a token's life", () => {
    // The setting means tolerance; read literally a negative one would expire
    // a token early, which is the opposite.
    expect(() => validateClaims({ exp: NOW + 1 }, config({ clock_skew_seconds: -600 }), NOW)).not.toThrow();
    expect(() => validateClaims({ exp: NOW - 1 }, config({ clock_skew_seconds: -600 }), NOW)).toThrow(/expired/);
  });

  it("checks the issuer only when one is configured", () => {
    expect(() => validateClaims({ exp: NOW + 60, iss: "anything" }, config(), NOW)).not.toThrow();
    expect(() => validateClaims({ exp: NOW + 60 }, config({ issuer: "https://idp" }), NOW)).toThrow(/invalid issuer/);
  });

  it("accepts an audience list containing the configured one", () => {
    // A token minted for several audiences is still minted for this one.
    expect(() =>
      validateClaims({ exp: NOW + 60, aud: ["other", "uterm"] }, config({ audience: "uterm" }), NOW),
    ).not.toThrow();
    expect(() => validateClaims({ exp: NOW + 60, aud: ["other"] }, config({ audience: "uterm" }), NOW)).toThrow(
      /invalid audience/,
    );
  });

  it("names each failure separately", () => {
    // The four are diagnosed differently: an expired token is a client that
    // needs to refresh, a wrong issuer is a misconfiguration.
    expect(raised(() => checkExp({ exp: NOW - 60 }, NOW, 0))).toEqual(golden.exp_alone);
    expect(raised(() => checkNbf({ nbf: NOW + 60 }, NOW, 0))).toEqual(golden.nbf_alone);
    expect(raised(() => checkIssuer({ iss: "a" }, config({ issuer: "b" })))).toEqual(golden.issuer_alone);
    expect(raised(() => checkAudience({ aud: "a" }, config({ audience: "b" })))).toEqual(golden.audience_alone);
  });
});

describe("reading a roles claim", () => {
  it.each(golden.roles_claim)("$name", (record) => {
    // A null inside the list is the one case the two runtimes cannot agree
    // on — see the divergence below.
    const expected = record.result.map((role) => (role === "None" ? "null" : role));
    expect([...parseRolesClaim(record.raw)]).toEqual(expected);
  });

  it("stringifies a null member differently from the reference", () => {
    // `str(None)` is "None" and `String(null)` is "null". Recorded rather
    // than papered over — and inert either way, because neither is a role
    // this system knows, so the principal resolves identically.
    expect([...parseRolesClaim(["admin", null])]).toEqual(["admin", "null"]);
    expect(golden.roles_claim.find((entry) => entry.name === "a list with a null")?.result).toEqual(["admin", "None"]);
    expect(resolveRole({ subjectId: "u", roles: ["admin", "null"] })).toBe(
      resolveRole({ subjectId: "u", roles: ["admin", "None"] }),
    );
  });

  it("splits a string on commas and drops the blanks", () => {
    expect([...parseRolesClaim("admin, operator ,")]).toEqual(["admin", "operator"]);
  });

  it("does not split on spaces", () => {
    // That shape is a scope, read elsewhere. Splitting it here would turn one
    // role into two.
    expect([...parseRolesClaim("admin operator")]).toEqual(["admin operator"]);
  });

  it("yields nothing for a shape that is not a claim", () => {
    for (const raw of [7, true, null, undefined, { admin: true }]) {
      expect([...parseRolesClaim(raw)]).toEqual([]);
    }
  });
});

describe("deriving roles from a token", () => {
  it.each(golden.roles)("$name", (record) => {
    expect([...extractRoles(record.claims, config(record.config))]).toEqual(record.result);
  });

  it("prefers roles over scope", () => {
    // Both present means the deployment set a roles claim, and that is the
    // authoritative one.
    expect([...extractRoles({ roles: ["viewer"], scope: "admin" }, config())]).toEqual(["viewer"]);
  });

  it("falls back to the scope, split on spaces", () => {
    expect([...extractRoles({ scope: "operator viewer" }, config())]).toEqual(["operator", "viewer"]);
  });

  it("falls back to the configured default when a token says nothing", () => {
    // A Cloudflare Access token carries no roles at all, so without this
    // every such user would have none.
    expect([...extractRoles({}, config())]).toEqual([golden.default_role]);
    expect([...extractRoles({}, config({ jwt_default_role: "operator" }))]).toEqual(["operator"]);
  });

  it("never leaves a principal with no role at all", () => {
    // An empty configured default still yields viewer rather than nothing.
    expect([...extractRoles({}, config({ jwt_default_role: "" }))]).toEqual(["viewer"]);
  });

  it("reads the claims the deployment named", () => {
    expect([...extractRoles({ groups: ["eng"] }, config({ jwt_roles_claim: "groups" }))]).toEqual(["eng"]);
    expect([...extractRoles({ scp: "admin" }, config({ jwt_scopes_claim: "scp" }))]).toEqual(["admin"]);
  });

  it("maps a group to a role, leaving unmapped ones alone", () => {
    const mapped = config({ jwt_roles_claim: "groups", jwt_role_map: { eng: "admin" } });
    expect([...extractRoles({ groups: ["eng"] }, mapped)]).toEqual(["admin"]);
    expect([...extractRoles({ groups: ["sales"] }, mapped)]).toEqual(["sales"]);
  });
});

describe("the one role a principal acts as", () => {
  it.each(golden.resolve)("$name", (record) => {
    expect(resolveRole({ subjectId: "u", roles: record.roles })).toBe(record.result);
  });

  it("takes the highest, whatever order they arrive in", () => {
    expect(resolveRole({ subjectId: "u", roles: ["viewer", "operator", "admin"] })).toBe("admin");
    expect(resolveRole({ subjectId: "u", roles: ["admin", "viewer"] })).toBe("admin");
    expect(resolveRole({ subjectId: "u", roles: ["viewer", "operator"] })).toBe("operator");
  });

  it("falls closed on a role it does not know", () => {
    expect(resolveRole({ subjectId: "u", roles: ["root"] })).toBe("viewer");
    expect(resolveRole({ subjectId: "u", roles: [] })).toBe("viewer");
  });
});

describe("finding the token on a request", () => {
  it.each(golden.bearer)("$name", (record) => {
    expect(extractBearerOrCookie(BEARER_REQUESTS[record.name]) ?? null).toBe(record.result);
  });

  it("works against the runtime's own Headers, which trims as it stores", () => {
    // A real request's headers are normalised before this ever sees them, so
    // the trailing space in "Bearer " never survives the wire. Both readers
    // have to be right: the corpus pins the untrimmed reference, this pins
    // the runtime.
    expect(extractBearerOrCookie({ headers: new Headers({ Authorization: "Bearer  abc " }) })).toBe("abc");
    expect(extractBearerOrCookie({ headers: new Headers({ Authorization: "Bearer " }) })).toBeUndefined();
    expect(extractBearerOrCookie({ headers: new Headers({ Cookie: " CF_Authorization = abc " }) })).toBe("abc");
  });

  it("reads the bearer header whatever case the scheme is in", () => {
    for (const scheme of ["Bearer", "bearer", "BEARER", "BeArEr"]) {
      expect(extractBearerOrCookie({ headers: new Headers({ Authorization: `${scheme} abc` }) })).toBe("abc");
    }
  });

  it("does not take a scheme that merely starts the same", () => {
    expect(extractBearerOrCookie({ headers: new Headers({ Authorization: "Bearerabc" }) })).toBeUndefined();
  });

  it("reads the access cookie, because a browser WebSocket cannot send headers", () => {
    // The only mechanism available on a WS upgrade behind Cloudflare Access.
    expect(extractBearerOrCookie({ headers: new Headers({ Cookie: "a=1; CF_Authorization=abc; b=2" }) })).toBe("abc");
  });

  it("matches the cookie by its whole name", () => {
    expect(extractBearerOrCookie({ headers: new Headers({ Cookie: "XCF_Authorization=abc" }) })).toBeUndefined();
  });

  it("skips a cookie carrying no value at all", () => {
    // Read as a pair, a valueless cookie one character longer would match a
    // name one character shorter and answer with its own name.
    expect(extractBearerOrCookie(rawHeaders({ Cookie: "CF_Authorizations; CF_Authorization=abc" }))).toBe("abc");
  });

  it("keeps an equals sign inside the cookie value", () => {
    // A JWT is base64url and carries none, but padding a value would.
    expect(extractBearerOrCookie({ headers: new Headers({ Cookie: "CF_Authorization=a=b" }) })).toBe("a=b");
  });

  it("prefers the header, falling through when it carries nothing", () => {
    // The header is what this request presented; the cookie is whatever the
    // browser happened to hold.
    const both = { headers: new Headers({ Authorization: "Bearer hdr", Cookie: "CF_Authorization=cke" }) };
    expect(extractBearerOrCookie(both)).toBe("hdr");
    const empty = { headers: new Headers({ Authorization: "Bearer ", Cookie: "CF_Authorization=cke" }) };
    expect(extractBearerOrCookie(empty)).toBe("cke");
  });

  it("yields nothing rather than raising", () => {
    // It runs on the request path, where an exception would be a 500 for a
    // request that simply had no token.
    expect(
      extractBearerOrCookie({
        headers: {
          get(): string {
            throw new Error("headers unreadable");
          },
        },
      }),
    ).toBeUndefined();
    expect(extractBearerOrCookie({})).toBeUndefined();
    expect(extractBearerOrCookie(null)).toBeUndefined();
  });
});
