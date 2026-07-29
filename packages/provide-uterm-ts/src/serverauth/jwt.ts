//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The JWT the server authenticates every request with.
 *
 * Port of the slice of PyJWT that `provide.uterm.server.auth` drives:
 * `jwt.decode(token, key, algorithms=..., issuer=..., audience=...,
 * leeway=..., options={"require": ["sub", "exp"]})`.
 *
 * Written out rather than taken from a library because the *refusals* are the
 * contract. Every one of the ways a token can be wrong — expired, signed by
 * somebody else, minted for another audience, carrying an algorithm the
 * deployment did not allow — has to be caught here, and `serverjwt_golden`
 * records what the reference does with each of them. A validator that agreed
 * about the accepting case and differed about one refusing case would be an
 * authentication bypass that every conformance scenario passes.
 *
 * `code` on a refusal is PyJWT's own exception class name, so the corpus
 * compares refusal *kinds* and not merely the fact of one.
 */

import { hmac } from "@noble/hashes/hmac.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { pyB64UrlDecode } from "../pycompat/base64.ts";

/** A token this server will not act on, and PyJWT's name for the reason. */
export class JwtError extends Error {
  /** The reference's exception class name, e.g. `ExpiredSignatureError`. */
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "JwtError";
    this.code = code;
  }
}

/** How a token is read. Mirrors the reference's `jwt.decode` arguments. */
export interface JwtDecodeOptions {
  /** The HMAC shared secret. Only symmetric keys are supported here. */
  key: string;
  /** The algorithms this deployment allows, from `auth.jwt_algorithms`. */
  algorithms: readonly string[];
  /** The issuer every token must name, or nothing to accept any. */
  issuer?: string | undefined;
  /** The audience every token must name, or nothing to accept any. */
  audience?: string | undefined;
  /** Clock skew tolerated either side, in seconds. */
  leeway?: number | undefined;
  /** Claims that must be present and non-null. */
  require?: readonly string[] | undefined;
  /** The current time in seconds. The runtime's clock unless a test says otherwise. */
  now?: (() => number) | undefined;
}

/** The claims of a token that verified. */
export type JwtClaims = Record<string, unknown>;

/** The only algorithm this server signs and verifies with. */
const HS256 = "HS256";

/** Base64url with the padding stripped, as a JWT segment is written. */
function b64urlEncode(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

/**
 * Sign a claim set as an HS256 token.
 *
 * Byte-for-byte what `jwt.encode(claims, secret, algorithm="HS256")` produces:
 * the header's two keys sorted (the reference's `sort_headers` default), the
 * claims in the order they were written, both serialised without spaces. The
 * corpus holds tokens minted by the reference, so an encoder that differed
 * anywhere — a space, a key order — would be visible rather than assumed.
 */
export function encodeJwt(claims: JwtClaims, secret: string): string {
  const bytes = new TextEncoder();
  const header = b64urlEncode(bytes.encode(JSON.stringify({ alg: HS256, typ: "JWT" })));
  const payload = b64urlEncode(bytes.encode(JSON.stringify(claims)));
  const signature = hmac(sha256, bytes.encode(secret), bytes.encode(`${header}.${payload}`));
  return `${header}.${payload}.${b64urlEncode(signature)}`;
}

/** Decode one segment, turning the decoder's refusal into PyJWT's wording. */
function segment(text: string, what: string): Uint8Array {
  try {
    return pyB64UrlDecode(text);
  } catch {
    throw new JwtError("DecodeError", `Invalid ${what} padding`);
  }
}

/** Parse one decoded segment as JSON, in PyJWT's two-step wording. */
function jsonSegment(bytes: Uint8Array, what: string): unknown {
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch (error) {
    throw new JwtError("DecodeError", `Invalid ${what} string: ${(error as Error).message}`);
  }
  // A header or payload that is a list, a number or a string parses fine and
  // is still not a claim set.
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new JwtError("DecodeError", `Invalid ${what} string: must be a json object`);
  }
  return parsed;
}

/** The three segments, split the way the reference splits them. */
function split(token: string): [string, string, string] {
  // Not `token.split(".")`: the reference takes the signature off the end and
  // the header off the front, so a fourth segment lands *inside* the payload
  // and is refused as bad padding rather than as a missing segment.
  const lastDot = token.lastIndexOf(".");
  const firstDot = token.indexOf(".");
  if (lastDot === -1 || firstDot === lastDot) {
    throw new JwtError("DecodeError", "Not enough segments");
  }
  return [token.slice(0, firstDot), token.slice(firstDot + 1, lastDot), token.slice(lastDot + 1)];
}

/** Whether two byte strings are equal, without telling anyone how far it got. */
function sameBytes(left: Uint8Array, right: Uint8Array): boolean {
  // Every byte of the expected digest is looked at whatever the supplied one
  // holds: a verifier that returned early would leak a signature one byte at
  // a time to whoever kept guessing.
  let differs = left.length ^ right.length;
  for (const [index, byte] of right.entries()) {
    differs |= (left[index] ?? 0) ^ byte;
  }
  return differs === 0;
}

/** Read a claim as an integer, as the reference's `int(...)` does. */
function integerClaim(value: unknown, code: string, message: string): number {
  const number = typeof value === "number" ? value : Number(String(value));
  if (!Number.isFinite(number)) {
    throw new JwtError(code, message);
  }
  return Math.trunc(number);
}

/** Every claim the caller said must be there, checked before any of them is read. */
function requiredClaims(claims: JwtClaims, required: readonly string[]): void {
  for (const name of required) {
    if (claims[name] === undefined || claims[name] === null) {
      throw new JwtError("MissingRequiredClaimError", `Token is missing the "${name}" claim`);
    }
  }
}

/** The `iat`, `nbf` and `exp` checks, in the order the reference runs them. */
function timeClaims(claims: JwtClaims, now: number, leeway: number): void {
  if ("iat" in claims) {
    const iat = integerClaim(claims.iat, "InvalidIssuedAtError", "Issued At claim (iat) must be an integer.");
    if (iat > now + leeway) {
      throw new JwtError("ImmatureSignatureError", "The token is not yet valid (iat)");
    }
  }
  if ("nbf" in claims) {
    const nbf = integerClaim(claims.nbf, "DecodeError", "Not Before claim (nbf) must be an integer.");
    if (nbf > now + leeway) {
      throw new JwtError("ImmatureSignatureError", "The token is not yet valid (nbf)");
    }
  }
  if ("exp" in claims) {
    const exp = integerClaim(claims.exp, "DecodeError", "Expiration Time claim (exp) must be an integer.");
    if (exp <= now - leeway) {
      throw new JwtError("ExpiredSignatureError", "Signature has expired");
    }
  }
}

/** The issuer check: a token that names nobody is as wrong as one that names another. */
function issuerClaim(claims: JwtClaims, issuer: string | undefined): void {
  if (issuer === undefined) {
    return;
  }
  if (!("iss" in claims)) {
    throw new JwtError("MissingRequiredClaimError", 'Token is missing the "iss" claim');
  }
  if (typeof claims.iss !== "string") {
    throw new JwtError("InvalidIssuerError", "Payload Issuer (iss) must be a string");
  }
  if (claims.iss !== issuer) {
    throw new JwtError("InvalidIssuerError", "Invalid issuer");
  }
}

/**
 * The audience check.
 *
 * A token minted for another service must not be replayable here, which is
 * the whole reason the claim is verified rather than read.
 */
function audienceClaim(claims: JwtClaims, audience: string | undefined): void {
  const claimed = claims.aud;
  const absent = !("aud" in claims) || claimed === "" || claimed === null || claimed === undefined;
  if (audience === undefined) {
    if (absent) {
      return;
    }
    throw new JwtError("InvalidAudienceError", "Invalid audience");
  }
  if (absent) {
    throw new JwtError("MissingRequiredClaimError", 'Token is missing the "aud" claim');
  }
  const claims_ = typeof claimed === "string" ? [claimed] : claimed;
  if (!Array.isArray(claims_) || claims_.some((one) => typeof one !== "string")) {
    throw new JwtError("InvalidAudienceError", "Invalid claim format in token");
  }
  if (!claims_.includes(audience)) {
    throw new JwtError("InvalidAudienceError", "Audience doesn't match");
  }
}

/**
 * Verify a token and return its claims.
 *
 * The order of the checks is the reference's: the signature before any claim,
 * and among the claims the required ones, then the clock, then the issuer,
 * then the audience. A token with two things wrong therefore fails on the
 * same one in both implementations, which is what makes a refusal comparable.
 *
 * @throws {JwtError} With PyJWT's exception name as its `code`.
 */
export function decodeJwt(token: string, options: JwtDecodeOptions): JwtClaims {
  const [headerSegment, payloadSegment, signatureSegment] = split(token);

  // All three segments are decoded before anything is decided about them,
  // exactly as the reference's loader does. The order is observable: a token
  // whose payload is unpaddable *and* whose signature is wrong is refused for
  // the padding, and a port that verified first would say the other thing.
  const header = jsonSegment(segment(headerSegment, "header"), "header") as JwtClaims;
  const payload = segment(payloadSegment, "payload");
  const signature = segment(signatureSegment, "crypto");

  const algorithm = header.alg;
  if (algorithm === undefined) {
    throw new JwtError("InvalidAlgorithmError", "Algorithm not specified");
  }
  if (algorithm === "" || !options.algorithms.includes(String(algorithm))) {
    throw new JwtError("InvalidAlgorithmError", "The specified alg value is not allowed");
  }
  // Only the symmetric algorithm the stub IdP mints is verifiable here. An
  // asymmetric one is refused rather than skipped — the one thing a verifier
  // must never do is let a token through because it could not check it.
  //
  // The wording is the reference's, and for the same reason: what it has to
  // work with is a shared secret, and a shared secret is not a public key.
  // A deployment that configured RS256 against a real PEM is not supported by
  // this port either, and fails closed here rather than anywhere later.
  if (algorithm !== HS256) {
    throw new JwtError("InvalidKeyError", "Could not parse the provided public key.");
  }

  const bytes = new TextEncoder();
  const expected = hmac(sha256, bytes.encode(options.key), bytes.encode(`${headerSegment}.${payloadSegment}`));
  if (!sameBytes(signature, expected)) {
    throw new JwtError("InvalidSignatureError", "Signature verification failed");
  }

  const claims = jsonSegment(payload, "payload") as JwtClaims;
  requiredClaims(claims, options.require ?? []);
  timeClaims(claims, (options.now ?? (() => Date.now() / 1000))(), options.leeway ?? 0);
  issuerClaim(claims, options.issuer);
  audienceClaim(claims, options.audience);
  return claims;
}
