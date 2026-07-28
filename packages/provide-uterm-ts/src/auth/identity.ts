//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * SSH public key to application identity.
 *
 * Port of the Python module `provide.uterm.auth`.
 *
 * This is the boundary between *transport* — the SSH handshake and the key it
 * carries, which this project owns — and *identity*, which the consuming
 * application owns. Two things have to be exact:
 *
 * - **The fingerprint.** It is the whole basis of the match, and it has to be
 *   the string `ssh-keygen -lf` prints. Fingerprint a different set of bytes
 *   and every key stops resolving; collide two and they become one identity.
 * - **The `authorized_keys` grammar.** The options field ends at the first
 *   whitespace *outside* quotes, so `command="echo hi",no-pty` is one token.
 *   Splitting inside one would read `no-pty` as the key type and refuse the
 *   line — locking that key out.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

/** An identity resolved from an SSH public key. */
export interface ResolvedIdentity {
  /** Opaque, consumer-defined identifier. Never parsed here. */
  subject: string;
  /** Whatever else the consumer wants carried along. */
  claims: Record<string, unknown>;
  /** The fingerprint that resolved, so a caller knows which key let them in. */
  fingerprint: string;
}

/** What a resolver is told about the connection. */
export interface ResolveContext {
  /** The full public key, for a resolver that wants more than the fingerprint. */
  pubkeyBlob: Uint8Array;
  /** The username offered at login. May be empty. */
  username: string;
}

/** Maps an SSH public key to an application identity. */
export interface SSHKeyResolver {
  /** The identity for this key, or nothing when the key is unknown. */
  resolve(fingerprint: string, context: ResolveContext): Promise<ResolvedIdentity | undefined>;
}

/** Key-type prefixes that mark the OpenSSH text form. */
const KEY_TYPE_PREFIXES = ["ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-"];

/** The prefix `ssh-keygen -lf` prints. */
const FINGERPRINT_PREFIX = "SHA256:";

/** Base64 alphabet, for the strict decode the reference performs. */
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

/** Whether a code point is whitespace, the way `str.isspace` counts it. */
function isSpace(character: string): boolean {
  return /\s/.test(character);
}

/** Decode base64, refusing anything the strict decoder would. */
function decodeBase64(text: string): Uint8Array {
  if (!BASE64.test(text) || text.length % 4 !== 0) {
    throw new Error("invalid base64 in public key: Only base64 data is allowed");
  }
  return Uint8Array.from(Buffer.from(text, "base64"));
}

/**
 * The wire-format bytes inside `blob`.
 *
 * Accepts the OpenSSH text form or the raw wire format. A prefix it does not
 * recognise is treated as raw bytes — which is why the prefix list has to
 * match the reference's, or the *text* gets fingerprinted instead of the key.
 */
function coerceToBinaryPubkey(blob: Uint8Array): Uint8Array {
  const text = Buffer.from(blob).toString("latin1").trim();
  if (KEY_TYPE_PREFIXES.some((prefix) => text.startsWith(prefix))) {
    // Only the first token after the key type is the payload; anything after
    // it is a comment, even when it looks like more base64.
    const parts = text.split(/\s+/, 2);
    if (parts.length < 2 || parts[1] === undefined || parts[1] === "") {
      throw new Error("malformed OpenSSH public key line");
    }
    return decodeBase64(parts[1]);
  }
  return Uint8Array.from(Buffer.from(text, "latin1"));
}

/**
 * The OpenSSH-style SHA-256 fingerprint of a public key.
 *
 * Base64 of the digest with the padding stripped, which is what
 * `ssh-keygen -lf` prints — padded base64, or hex, would be a fingerprint
 * nobody can paste from their own key.
 *
 * @throws {Error} If the blob cannot be read as a public key.
 */
export function fingerprintFromOpensshBlob(blob: Uint8Array | string): string {
  const bytes = typeof blob === "string" ? Uint8Array.from(Buffer.from(blob, "utf8")) : blob;
  const binary = coerceToBinaryPubkey(bytes);
  const digest = createHash("sha256").update(binary).digest("base64");
  return `${FINGERPRINT_PREFIX}${digest.replace(/=+$/, "")}`;
}

/**
 * A resolver that never resolves anything.
 *
 * Exists so a caller can always pass one; resolving would be the opposite of
 * what it is for.
 */
export class NullResolver implements SSHKeyResolver {
  /** Always nothing. */
  async resolve(_fingerprint: string, _context: ResolveContext): Promise<ResolvedIdentity | undefined> {
    return undefined;
  }
}

/** One parsed line of an `authorized_keys` file. */
export interface AuthorizedKeyEntry {
  /** The key's fingerprint. */
  fingerprint: string;
  /** Who the key belongs to. */
  subject: string;
  /** Everything else the line carried. */
  claims: Record<string, unknown>;
}

/**
 * Where the first top-level whitespace is.
 *
 * Quoted substrings are respected, so `command="echo hi",no-pty` is one
 * token. Backslash escapes are not interpreted — OpenSSH's own parser does
 * not interpret them inside option values either.
 */
function findFirstTokenEnd(line: string): number {
  let inQuotes = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index] as string;
    if (character === '"') {
      inQuotes = !inQuotes;
    } else if (isSpace(character) && !inQuotes) {
      return index;
    }
  }
  return line.length;
}

/** Split an options field on the commas that are not inside quotes. */
function splitOptions(optionsText: string): string[] {
  const out: string[] = [];
  let buffer = "";
  let inQuotes = false;
  for (const character of optionsText) {
    if (character === '"') {
      inQuotes = !inQuotes;
      buffer += character;
    } else if (character === "," && !inQuotes) {
      // An empty run between commas is skipped rather than becoming a flag.
      if (buffer !== "") {
        out.push(buffer);
        buffer = "";
      }
    } else {
      buffer += character;
    }
  }
  if (buffer !== "") {
    out.push(buffer);
  }
  return out;
}

/** Parse the options field into a map. `flag` becomes `true`. */
function parseOptions(optionsText: string): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (const token of splitOptions(optionsText)) {
    const equals = token.indexOf("=");
    if (equals >= 0) {
      // Everything after the *first* `=` is the value, so `environment=FOO=bar`
      // keeps its own assignment.
      const key = token.slice(0, equals).trim();
      out[key] = token
        .slice(equals + 1)
        .trim()
        .replace(/^"|"$/g, "");
    } else {
      out[token.trim()] = true;
    }
  }
  return out;
}

/**
 * Parse one non-empty, non-comment line of an `authorized_keys` file.
 *
 * The subject falls back from the `subject=` option to the comment to
 * `key:<fingerprint>`, so every key gets one and none is silently
 * unresolvable.
 *
 * @throws {Error} If the line carries no key payload.
 */
export function parseAuthorizedKeysLine(line: string): AuthorizedKeyEntry {
  const firstTokenEnd = findFirstTokenEnd(line);
  const firstToken = line.slice(0, firstTokenEnd);
  const hasOptions = !KEY_TYPE_PREFIXES.some((prefix) => firstToken.startsWith(prefix));
  const optionsText = hasOptions ? firstToken : "";
  const rest = hasOptions ? line.slice(firstTokenEnd).replace(/^\s+/, "") : line;

  const parts = rest.split(/\s+/);
  if (parts.length < 2 || parts[1] === undefined || parts[1] === "") {
    throw new Error("missing key payload");
  }
  const [keytype, payload] = parts;
  const comment = parts.slice(2).join(" ");

  const fingerprint = fingerprintFromOpensshBlob(`${keytype} ${payload}`);

  const options = optionsText === "" ? {} : parseOptions(optionsText);
  const subjectOption = options.subject;
  delete options.subject;
  const subject =
    typeof subjectOption === "string" && subjectOption !== "" ? subjectOption : comment.trim() || `key:${fingerprint}`;

  const claims: Record<string, unknown> = {};
  const leftover: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(options)) {
    if (key.startsWith("claim-")) {
      claims[key.slice("claim-".length)] = value;
    } else {
      leftover[key] = value;
    }
  }
  // Preserved rather than dropped, so a consumer can still see `no-pty`.
  if (Object.keys(leftover).length > 0) {
    claims._options = leftover;
  }

  return { fingerprint, subject, claims };
}

/**
 * Resolve identities against a file in OpenSSH `authorized_keys` format.
 *
 * The file is read on every call, so a key rotation takes effect immediately
 * rather than at the next restart.
 */
export class AuthorizedKeysFileResolver implements SSHKeyResolver {
  readonly #path: string;

  constructor(path: string) {
    this.#path = path;
  }

  /** The identity for this key, or nothing when the file does not name it. */
  async resolve(fingerprint: string, _context: ResolveContext): Promise<ResolvedIdentity | undefined> {
    for (const entry of this.#entries()) {
      if (entry.fingerprint === fingerprint) {
        return { subject: entry.subject, claims: entry.claims, fingerprint };
      }
    }
    return undefined;
  }

  /** Parse the file, skipping what it cannot read. */
  #entries(): AuthorizedKeyEntry[] {
    let text: string;
    try {
      text = readFileSync(this.#path, "utf8");
    } catch {
      // A gateway that has not been given a key file yet should refuse keys,
      // not fail to start.
      return [];
    }
    const out: AuthorizedKeyEntry[] = [];
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (line === "" || line.startsWith("#")) {
        continue;
      }
      try {
        out.push(parseAuthorizedKeysLine(line));
      } catch {
        // One bad entry must not lock everybody out, so the file is read
        // entry by entry rather than all or nothing.
      }
    }
    return out;
  }
}
