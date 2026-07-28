//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import type { JwtConfig } from "./index.ts";
import { decodeJwt, defaultConfig, JwksCache, JwtValidationError, requestJwks, verifyRs256 } from "./index.ts";

interface VerifyGolden {
  cache_ttl_s: number;
  negative_ttl_s: number;
  cache_steps: Array<{
    name: string;
    elapsed: number;
    endpoint_failing: boolean;
    fetched: boolean;
    keys: unknown;
    error: string | null;
  }>;
  first_fetch_failure: { error: string | null; calls: number };
  separate_urls: { first: unknown; second: unknown; again: unknown; calls: number };
  principals: Array<{
    name: string;
    claims: Record<string, unknown>;
    config: Record<string, unknown>;
    subject_id: string | null;
    roles: string[] | null;
    error: string | null;
  }>;
  unconfigured: { error: string | null };
  verification_failure: { error: string | null };
}

const golden = loadGolden<VerifyGolden>("cfjwtverify_golden.json");

/** A key as `crypto.subtle` hands it back. */
type SigningKey = Awaited<ReturnType<typeof crypto.subtle.importKey>>;
const JWKS_URL = "https://idp/.well-known/jwks.json";

/** The default configuration with a JWKS endpoint and a field or two changed. */
function config(overrides: Record<string, unknown> = {}): JwtConfig {
  return { ...defaultConfig().jwt, jwks_url: JWKS_URL, ...overrides } as JwtConfig;
}

/** A JWKS endpoint that answers, or does not. */
class Endpoint {
  calls = 0;
  failing = false;
  payload: unknown = { keys: [{ kid: "k1" }] };

  fetch = async (_url: string): Promise<unknown> => {
    this.calls += 1;
    if (this.failing) {
      throw new Error("jwks endpoint down");
    }
    return this.payload;
  };
}

/** A monotonic clock the test advances by hand. */
function clockFrom(start: number): { now: () => number; advance: (by: number) => void } {
  let value = start;
  return { now: () => value, advance: (by) => (value += by) };
}

describe("the JWKS cache", () => {
  it("walks failure and recovery exactly as the reference does", async () => {
    // One endpoint, one timeline: fetched or served from cache at each step.
    const endpoint = new Endpoint();
    const clock = clockFrom(1000);
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clock.now });
    let previous = 0;

    for (const step of golden.cache_steps) {
      clock.advance(step.elapsed - previous);
      previous = step.elapsed;
      endpoint.failing = step.endpoint_failing;
      const before = endpoint.calls;
      const result = await cache.get(JWKS_URL);
      expect({ fetched: endpoint.calls > before, keys: (result as { keys: unknown }).keys }).toEqual({
        fetched: step.fetched,
        keys: step.keys,
      });
    }
  });

  it("serves a cached copy without touching the endpoint", async () => {
    const endpoint = new Endpoint();
    const clock = clockFrom(0);
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clock.now });
    await cache.get(JWKS_URL);
    await cache.get(JWKS_URL);
    await cache.get(JWKS_URL);
    expect(endpoint.calls).toBe(1);
  });

  it("expires exactly at the ttl, not a moment later", async () => {
    // The comparison is strict, so a copy the full TTL old is stale.
    const endpoint = new Endpoint();
    const clock = clockFrom(0);
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clock.now });
    await cache.get(JWKS_URL);
    clock.advance(golden.cache_ttl_s);
    await cache.get(JWKS_URL);
    expect(endpoint.calls).toBe(2);
  });

  it("refreshes once the copy is stale", async () => {
    const endpoint = new Endpoint();
    const clock = clockFrom(0);
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clock.now });
    await cache.get(JWKS_URL);
    clock.advance(golden.cache_ttl_s - 1);
    await cache.get(JWKS_URL);
    expect(endpoint.calls).toBe(1);
    clock.advance(2);
    await cache.get(JWKS_URL);
    expect(endpoint.calls).toBe(2);
  });

  it("serves the stale copy when a refresh fails", async () => {
    // A flapping endpoint must not take down every authenticated request.
    const endpoint = new Endpoint();
    const clock = clockFrom(0);
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clock.now });
    await cache.get(JWKS_URL);
    clock.advance(golden.cache_ttl_s + 1);
    endpoint.failing = true;
    expect(await cache.get(JWKS_URL)).toEqual({ keys: [{ kid: "k1" }] });
  });

  it("backs off before trying a known-bad endpoint again", async () => {
    // Otherwise a down endpoint is re-hit on every single request.
    const endpoint = new Endpoint();
    const clock = clockFrom(0);
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clock.now });
    await cache.get(JWKS_URL);
    clock.advance(golden.cache_ttl_s + 1);
    endpoint.failing = true;
    await cache.get(JWKS_URL);
    const afterFirstFailure = endpoint.calls;

    clock.advance(golden.negative_ttl_s - 1);
    await cache.get(JWKS_URL);
    expect(endpoint.calls).toBe(afterFirstFailure);

    clock.advance(2);
    await cache.get(JWKS_URL);
    expect(endpoint.calls).toBe(afterFirstFailure + 1);
  });

  it("stops backing off once the endpoint recovers", async () => {
    const endpoint = new Endpoint();
    const clock = clockFrom(0);
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clock.now });
    await cache.get(JWKS_URL);
    clock.advance(golden.cache_ttl_s + 1);
    endpoint.failing = true;
    await cache.get(JWKS_URL);

    clock.advance(golden.negative_ttl_s + 1);
    endpoint.failing = false;
    endpoint.payload = { keys: [{ kid: "k2" }] };
    expect(await cache.get(JWKS_URL)).toEqual({ keys: [{ kid: "k2" }] });

    // The backoff is cleared, so the next expiry refreshes immediately.
    clock.advance(golden.cache_ttl_s + 1);
    const before = endpoint.calls;
    await cache.get(JWKS_URL);
    expect(endpoint.calls).toBe(before + 1);
  });

  it("fails a first fetch that has nothing to fall back on", async () => {
    // Serving nothing is not an option: there is no copy to serve. The
    // endpoint's own error is reported, not whatever reading an absent entry
    // would have raised.
    const endpoint = new Endpoint();
    endpoint.failing = true;
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clockFrom(0).now });
    await expect(cache.get(JWKS_URL)).rejects.toThrow(/jwks endpoint down/);
    expect(endpoint.calls).toBe(golden.first_fetch_failure.calls);
    expect(golden.first_fetch_failure.error).not.toBeNull();
  });

  it("keeps endpoints apart", async () => {
    // One issuer's keys must never be served for another's.
    const endpoint = new Endpoint();
    const cache = new JwksCache({ fetch: endpoint.fetch, now: clockFrom(0).now });
    endpoint.payload = { keys: [{ kid: "a" }] };
    const first = await cache.get("https://a/jwks");
    endpoint.payload = { keys: [{ kid: "b" }] };
    const second = await cache.get("https://b/jwks");
    const again = await cache.get("https://a/jwks");
    expect((first as { keys: unknown }).keys).toEqual(golden.separate_urls.first);
    expect((second as { keys: unknown }).keys).toEqual(golden.separate_urls.second);
    expect((again as { keys: unknown }).keys).toEqual(golden.separate_urls.again);
    expect(endpoint.calls).toBe(golden.separate_urls.calls);
  });
});

describe("what a verified token becomes", () => {
  /** Decode with the signature check replaced by fixed claims. */
  async function principal(claims: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
    return decodeJwt("a.b.c", config(overrides), { verify: async () => claims });
  }

  it.each(golden.principals)("$name", async (record) => {
    if (record.error !== null) {
      await expect(principal(record.claims, record.config)).rejects.toThrow(record.error);
      return;
    }
    const result = await principal(record.claims, record.config);
    expect(result.subjectId).toBe(record.subject_id);
    expect([...result.roles]).toEqual(record.roles);
  });

  it("refuses a token that names nobody", async () => {
    // An unnamed principal cannot be audited, so there is nothing to grant.
    for (const claims of [{}, { sub: "" }, { sub: null }, { roles: ["admin"] }]) {
      await expect(principal(claims)).rejects.toThrow(/missing sub/);
    }
  });

  it("falls back to the common name as a subject", async () => {
    expect((await principal({ common_name: "ci.example" })).subjectId).toBe("ci.example");
  });

  it("grants a service token admin only where the deployment opted in", async () => {
    // A bare common name is too weak a signal to auto-grant admin.
    expect([...(await principal({ common_name: "ci" })).roles]).toEqual(["viewer"]);
    expect([...(await principal({ common_name: "ci" }, { jwt_service_token_admin: true })).roles]).toEqual(["admin"]);
  });

  it("needs a common name, not merely the absence of an email", async () => {
    // Without the name there is nothing identifying the token as automation,
    // so an ordinary user token that happens to carry no email must not be
    // elevated.
    const opted = { jwt_service_token_admin: true };
    expect([...(await principal({ sub: "u1", roles: ["viewer"] }, opted)).roles]).toEqual(["viewer"]);
  });

  it("never elevates a token carrying a human identity", async () => {
    // An email means a user token, whatever else it holds — so a user cannot
    // be promoted by presenting a common name.
    const opted = { jwt_service_token_admin: true };
    expect([...(await principal({ sub: "u1", common_name: "ci", email: "a@b" }, opted)).roles]).toEqual(["viewer"]);
  });

  it("treats an empty email as no email at all", async () => {
    // The reference tests the claim for truth, not presence, so an empty
    // string does not mark the token as human. Recorded because it is the
    // edge where the two readings differ.
    const opted = { jwt_service_token_admin: true };
    expect([...(await principal({ sub: "u1", common_name: "ci", email: "" }, opted)).roles]).toEqual(["admin"]);
    expect(golden.principals.find((entry) => entry.name === "a common name with an empty email")?.roles).toEqual([
      "admin",
    ]);
  });

  it("ignores a service token's own roles once it is admin", async () => {
    const opted = { jwt_service_token_admin: true };
    expect([...(await principal({ common_name: "ci", roles: ["viewer"] }, opted)).roles]).toEqual(["admin"]);
  });

  it("refuses verified claims that are not an object", async () => {
    // A verifier returning a bare value has nothing to name a principal
    // with, so there is nothing to grant.
    for (const value of [null, "a string", 7]) {
      await expect(decodeJwt("a.b.c", config(), { verify: async () => value })).rejects.toThrow(/missing sub/);
    }
  });

  it("stringifies a subject that is not a string", async () => {
    expect((await principal({ sub: 7 })).subjectId).toBe("7");
  });

  it("refuses a deployment with no key configured at all", async () => {
    // No dev bypass: without a key nothing can be verified, so nothing is.
    await expect(decodeJwt("a.b.c", { ...defaultConfig().jwt })).rejects.toThrow(golden.unconfigured.error as string);
  });

  it("reports a failure inside verification as a validation error", async () => {
    // The caller distinguishes "not authenticated" from "broken"; an
    // unusable key is the former as far as the request is concerned.
    await expect(
      decodeJwt("a.b.c", config(), {
        verify: async () => {
          throw new Error("the key was unusable");
        },
      }),
    ).rejects.toThrow(golden.verification_failure.error as string);
  });

  it("lets a validation error through unchanged", async () => {
    // Wrapping it would bury the reason the token was refused.
    await expect(
      decodeJwt("a.b.c", config(), {
        verify: async () => {
          throw new JwtValidationError("token has expired");
        },
      }),
    ).rejects.toThrow(/^token has expired$/);
  });
});

describe("checking a signature", () => {
  // The reference cannot test this: its Web Crypto path runs only inside the
  // Cloudflare Pyodide runtime and is marked no-cover, so its own tests cover
  // the wiring alone. `crypto.subtle` is native both here and in a Worker.

  /** A signing key, and the JWKS a verifier would fetch for it. */
  async function keyPair(kid = "k1") {
    // The pair type lives in the DOM library, which this package does not
    // take; it is read back off the call instead of named.
    const pair = (await crypto.subtle.generateKey(
      { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
      true,
      ["sign", "verify"],
    )) as { privateKey: SigningKey; publicKey: SigningKey };
    const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
    return { privateKey: pair.privateKey, jwks: { keys: [{ ...jwk, kid, alg: "RS256" }] } };
  }

  /** Sign a token with a private key. */
  async function sign(privateKey: SigningKey, header: unknown, payload: unknown): Promise<string> {
    const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
    const signingInput = `${encode(header)}.${encode(payload)}`;
    const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(signingInput));
    return `${signingInput}.${Buffer.from(signature).toString("base64url")}`;
  }

  const NOW = 1_700_000_000;
  const claims = { sub: "u1", exp: NOW + 60 };

  it("accepts a token signed by the advertised key", async () => {
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "RS256", kid: "k1" }, claims);
    expect(await verifyRs256(token, jwks, config(), NOW)).toMatchObject({ sub: "u1" });
  });

  it("refuses a token whose payload was altered after signing", async () => {
    // The whole point: the signature covers the bytes as sent.
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "RS256", kid: "k1" }, claims);
    const parts = token.split(".");
    const forged = Buffer.from(JSON.stringify({ ...claims, sub: "admin" })).toString("base64url");
    await expect(verifyRs256(`${parts[0]}.${forged}.${parts[2]}`, jwks, config(), NOW)).rejects.toThrow(
      /signature verification failed/,
    );
  });

  it("refuses a token signed by another key", async () => {
    const signer = await keyPair();
    const advertised = await keyPair();
    const token = await sign(signer.privateKey, { alg: "RS256", kid: "k1" }, claims);
    await expect(verifyRs256(token, advertised.jwks, config(), NOW)).rejects.toThrow(/signature verification failed/);
  });

  it("refuses a token with no signature at all", async () => {
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "RS256", kid: "k1" }, claims);
    const parts = token.split(".");
    await expect(verifyRs256(`${parts[0]}.${parts[1]}.`, jwks, config(), NOW)).rejects.toThrow(
      /signature verification failed/,
    );
  });

  it("refuses an algorithm the deployment does not allow", async () => {
    // Including `none`, which is the classic forgery: a token that asks not
    // to be checked.
    const { privateKey, jwks } = await keyPair();
    for (const alg of ["none", "HS256", "RS512"]) {
      const token = await sign(privateKey, { alg, kid: "k1" }, claims);
      await expect(verifyRs256(token, jwks, config(), NOW)).rejects.toThrow(/unsupported algorithm/);
    }
  });

  it("refuses an algorithm that is allowed but not implemented here", async () => {
    // A deployment configuring ES256 must be told so, not handed an RS256
    // check against an EC key.
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "ES256", kid: "k1" }, claims);
    await expect(verifyRs256(token, jwks, config({ algorithms: ["ES256"] }), NOW)).rejects.toThrow(
      /only supports RS256/,
    );
  });

  it("validates the claims as well as the signature", async () => {
    // A correctly signed token is not thereby a usable one.
    const { privateKey, jwks } = await keyPair();
    const expired = await sign(privateKey, { alg: "RS256", kid: "k1" }, { sub: "u1", exp: NOW - 3600 });
    await expect(verifyRs256(expired, jwks, config(), NOW)).rejects.toThrow(/expired/);

    const wrongIssuer = await sign(privateKey, { alg: "RS256", kid: "k1" }, { ...claims, iss: "https://evil" });
    await expect(verifyRs256(wrongIssuer, jwks, config({ issuer: "https://idp" }), NOW)).rejects.toThrow(
      /invalid issuer/,
    );
  });

  it("matches a key by algorithm when the token names none", async () => {
    // With no key id the algorithm is the only thing narrowing the choice,
    // so it has to reach the search — otherwise the first key wins whatever
    // it is for.
    const { privateKey } = await keyPair();
    const rsa = await keyPair();
    const jwks = { keys: [{ kty: "EC", alg: "ES256" }, ...(rsa.jwks.keys as unknown[])] };
    void privateKey;
    const token = await sign(rsa.privateKey, { alg: "RS256" }, claims);
    expect(await verifyRs256(token, jwks, config(), NOW)).toMatchObject({ sub: "u1" });
  });

  it("fetches over plain http as well as https", async () => {
    // A JWKS endpoint inside a private network is not always TLS-terminated
    // at the endpoint itself.
    const original = globalThis.fetch;
    globalThis.fetch = (async () => Response.json({ keys: [] })) as typeof fetch;
    try {
      expect(await requestJwks("http://idp/jwks")).toEqual({ keys: [] });
    } finally {
      globalThis.fetch = original;
    }
  });

  it("refuses a token naming a key the JWKS does not have", async () => {
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "RS256", kid: "rotated" }, claims);
    await expect(verifyRs256(token, jwks, config(), NOW)).rejects.toThrow(/no matching key/);
  });

  it("verifies end to end through the default path", async () => {
    // No injected verifier: the keys are fetched from the configured
    // endpoint and the signature is checked against them, which is what the
    // Worker actually does.
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "RS256", kid: "k1" }, claims);
    const cache = { get: async () => jwks };
    expect(await decodeJwt(token, config(), { jwks: cache, now: NOW })).toEqual({
      subjectId: "u1",
      roles: ["viewer"],
    });

    const forged = await sign((await keyPair()).privateKey, { alg: "RS256", kid: "k1" }, claims);
    await expect(decodeJwt(forged, config(), { jwks: cache, now: NOW })).rejects.toThrow(
      /signature verification failed/,
    );
  });

  it("says which setting is missing when it cannot fetch keys", async () => {
    // A deployment with only a static PEM configured has not configured
    // *this* verifier, and should be told that rather than failing on a
    // fetch of nothing.
    await expect(
      decodeJwt("a.b.c", { ...defaultConfig().jwt, public_key_pem: "-----BEGIN PUBLIC KEY-----" } as JwtConfig),
    ).rejects.toThrow(/jwks_url must be configured/);
  });

  it("refuses a JWKS URL that is not http", async () => {
    // The URL comes from configuration and should always be http(s);
    // checking turns the assumption into a preflight rather than opening
    // whatever scheme it names.
    await expect(requestJwks("file:///etc/passwd")).rejects.toThrow(/must be http/);
  });

  it("refuses a JWKS endpoint that answers with an error", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () => new Response("nope", { status: 503 })) as typeof fetch;
    try {
      await expect(requestJwks("https://idp/jwks")).rejects.toThrow(/JWKS fetch failed: 503/);
    } finally {
      globalThis.fetch = original;
    }
  });

  it("reads a JWKS endpoint that answers", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () => Response.json({ keys: [{ kid: "k1" }] })) as typeof fetch;
    try {
      expect(await requestJwks("https://idp/jwks")).toEqual({ keys: [{ kid: "k1" }] });
    } finally {
      globalThis.fetch = original;
    }
  });

  it("reads the wall clock when given none", async () => {
    // The Worker constructs it that way; the injected clock is for tests.
    const endpoint = new Endpoint();
    const cache = new JwksCache({ fetch: endpoint.fetch });
    expect(await cache.get(JWKS_URL)).toEqual({ keys: [{ kid: "k1" }] });
    expect(await cache.get(JWKS_URL)).toEqual({ keys: [{ kid: "k1" }] });
    expect(endpoint.calls).toBe(1);
  });

  it("refuses a header that is not an object", async () => {
    // It parses, so nothing upstream refuses it — the algorithm simply reads
    // as absent, and a token asking to be verified with no algorithm is not
    // one.
    const { jwks } = await keyPair();
    const header = Buffer.from("[1,2]").toString("base64url");
    const payload = Buffer.from(JSON.stringify(claims)).toString("base64url");
    await expect(verifyRs256(`${header}.${payload}.AQID`, jwks, config(), NOW)).rejects.toThrow(
      /unsupported algorithm/,
    );
  });

  it("treats a key id that is not a string as none given", async () => {
    // Rather than matching it against a key id that is one.
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "RS256", kid: 7 }, claims);
    expect(await verifyRs256(token, jwks, config(), NOW)).toMatchObject({ sub: "u1" });
  });

  it("uses the isolate's own cache when given none", async () => {
    // The default path end to end, including the module-level cache the
    // Worker relies on.
    const { privateKey, jwks } = await keyPair();
    const token = await sign(privateKey, { alg: "RS256", kid: "k1" }, claims);
    const original = globalThis.fetch;
    globalThis.fetch = (async () => Response.json(jwks)) as typeof fetch;
    try {
      expect(await decodeJwt(token, config({ jwks_url: "https://idp/live-jwks" }), { now: NOW })).toEqual({
        subjectId: "u1",
        roles: ["viewer"],
      });
    } finally {
      globalThis.fetch = original;
    }
  });

  it("refuses a header of null", async () => {
    // `null` parses and is an object to the engine, so the null check is the
    // only thing between it and a property read that would throw.
    const { jwks } = await keyPair();
    const header = Buffer.from("null").toString("base64url");
    const payload = Buffer.from(JSON.stringify(claims)).toString("base64url");
    await expect(verifyRs256(`${header}.${payload}.AQID`, jwks, config(), NOW)).rejects.toThrow(
      /unsupported algorithm/,
    );
  });

  it("refuses a key the runtime will not import", async () => {
    // A malformed JWK must be a refusal, not an exception escaping into the
    // request path.
    const token = await sign((await keyPair()).privateKey, { alg: "RS256", kid: "k1" }, claims);
    await expect(verifyRs256(token, { keys: [{ kid: "k1", kty: "oct" }] }, config(), NOW)).rejects.toThrow(
      JwtValidationError,
    );
  });
});
