//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Typed builder functions for control-channel protocol messages.
 *
 * Each builder returns a fresh object ready to pass to
 * `encodeControlFrame`. Required fields are validated; optional fields are
 * omitted when not supplied so the serialised JSON stays lean.
 *
 * Port of the Python module `provide.uterm.control_channel_builders`.
 */

import { createHmac } from "node:crypto";
import { pyJsonDumps } from "../pycompat/index.ts";

/** Optional inputs to {@link makeIdentity}. */
export interface MakeIdentityOptions {
  /** Additional identity claims. Omitted from the frame when not supplied. */
  claims?: Record<string, unknown>;
  /** SSH key fingerprint. Defaults to the empty string. */
  fingerprint?: string;
  /** Transport type. Defaults to `"ssh"`. */
  transport?: string;
  /** HMAC secret. An empty value leaves the frame unsigned. */
  secret?: string | Uint8Array;
}

/**
 * Build the byte string the identity signature is taken over.
 *
 * The claims segment is CPython-canonical JSON — sorted keys, compact
 * separators, `ensure_ascii` on — because the HMAC covers these exact bytes
 * and any difference is a rejected identity rather than a cosmetic one.
 */
function canonicalIdentitySignaturePayload(args: {
  version: number;
  subject: string;
  fingerprint: string;
  transport: string;
  claims: Record<string, unknown>;
}): Buffer {
  const claimsJson = pyJsonDumps(args.claims);
  return Buffer.from(`${args.version}:${args.subject}:${args.fingerprint}:${args.transport}:${claimsJson}`, "utf-8");
}

/**
 * Build an `identity` control message.
 *
 * @param subject Non-empty identity subject, e.g. `"user:alice"`.
 * @throws {Error} If `subject` is empty.
 */
export function makeIdentity(subject: string, options: MakeIdentityOptions = {}): Record<string, unknown> {
  if (subject === "") {
    throw new Error("make_identity: 'subject' must be a non-empty string");
  }
  const fingerprint = options.fingerprint ?? "";
  const transport = options.transport ?? "ssh";
  const message: Record<string, unknown> = {
    type: "identity",
    version: 1,
    subject,
    fingerprint,
    transport,
  };
  if (options.claims !== undefined) {
    message.claims = { ...options.claims };
  }

  const secret = options.secret;
  // An empty string or empty byte array is falsy in the reference, so it
  // leaves the frame unsigned rather than signing with an empty key.
  if (secret !== undefined && secret.length > 0) {
    const key = typeof secret === "string" ? Buffer.from(secret, "utf-8") : Buffer.from(secret);
    const payload = canonicalIdentitySignaturePayload({
      version: 1,
      subject,
      fingerprint,
      transport,
      claims: (message.claims as Record<string, unknown> | undefined) ?? {},
    });
    message.signature = createHmac("sha256", key).update(payload).digest("hex");
  }
  return message;
}

/**
 * Build a `session_token` control message.
 *
 * @throws {Error} If `token` is empty.
 */
export function makeSessionToken(token: string, playerId?: number): Record<string, unknown> {
  if (token === "") {
    throw new Error("make_session_token: 'token' must be a non-empty string");
  }
  const message: Record<string, unknown> = { type: "session_token", token };
  if (playerId !== undefined) {
    message.player_id = playerId;
  }
  return message;
}

/**
 * Build a `resume` control message.
 *
 * @throws {Error} If `token` is empty.
 */
export function makeResume(token: string, playerId?: number): Record<string, unknown> {
  if (token === "") {
    throw new Error("make_resume: 'token' must be a non-empty string");
  }
  const message: Record<string, unknown> = { type: "resume", token };
  if (playerId !== undefined) {
    message.player_id = playerId;
  }
  return message;
}

/** Build a `resume_ok` control message. */
export function makeResumeOk(): Record<string, unknown> {
  return { type: "resume_ok" };
}

/** Build a `resume_failed` control message. */
export function makeResumeFailed(reason?: string): Record<string, unknown> {
  const message: Record<string, unknown> = { type: "resume_failed" };
  if (reason !== undefined) {
    message.reason = reason;
  }
  return message;
}

/** Build a `presence_update` control message with arbitrary extra fields. */
export function makePresenceUpdate(userId: string, fields: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: "presence_update", user_id: userId, ...fields };
}

/** Fields `LinkPatternEntry` models, with the types the reference accepts. */
const LINK_PATTERN_FIELDS: Readonly<Record<string, (value: unknown) => boolean>> = {
  pattern: (value) => typeof value === "string",
  action: (value) => value === "cmd" || value === "url" || value === "key" || value === "focus",
  id: (value) => typeof value === "string",
  flags: (value) => typeof value === "string",
  // Modelled as `int | str | None`, so a string group is valid as written.
  group: (value) => typeof value === "number" || typeof value === "string",
  // Modelled as `Any`, so any JSON value survives.
  payload: () => true,
  hover: (value) => typeof value === "string",
  line_contains: (value) => typeof value === "string",
  class: (value) => typeof value === "string",
};

/** Fields that must be present on every entry. */
const LINK_PATTERN_REQUIRED = ["pattern", "action"] as const;

/**
 * Validate one link-pattern entry against the modelled field set.
 *
 * The reference model is `extra="forbid"`, so an unmodelled field is an error
 * rather than being silently dropped on the wire.
 */
function validateLinkPatternEntry(entry: Record<string, unknown>): string | undefined {
  for (const required of LINK_PATTERN_REQUIRED) {
    if (!(required in entry)) {
      return `${required} is required`;
    }
  }
  for (const [key, value] of Object.entries(entry)) {
    const check = LINK_PATTERN_FIELDS[key];
    if (check === undefined) {
      return `${key} is not a modelled field`;
    }
    if (value !== null && !check(value)) {
      return `${key} has the wrong type`;
    }
  }
  return undefined;
}

/**
 * Build a `link_patterns` control message.
 *
 * Each entry is validated against the modelled field set: `pattern` and
 * `action` are required, and an unmodelled field is refused rather than
 * dropped.
 *
 * @throws {Error} If any entry is malformed, naming the offending index.
 */
export function makeLinkPatterns(patterns: ReadonlyArray<Record<string, unknown>>): Record<string, unknown> {
  const entries: Array<Record<string, unknown>> = [];
  for (const [index, entry] of patterns.entries()) {
    const problem = validateLinkPatternEntry(entry);
    if (problem !== undefined) {
      throw new Error(`make_link_patterns: entry[${index}] is invalid: ${problem}`);
    }
    // Null-valued optional fields are dropped, matching exclude_none.
    const cleaned: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(entry)) {
      if (value !== null) {
        cleaned[key] = value;
      }
    }
    entries.push(cleaned);
  }
  return { type: "link_patterns", patterns: entries };
}
