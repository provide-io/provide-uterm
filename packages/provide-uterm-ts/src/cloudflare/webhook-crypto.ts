//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * At-rest encryption for webhook signing secrets.
 *
 * Port of the Python module
 * `provide.uterm.cloudflare.do._webhook_crypto`.
 *
 * A webhook's secret is an HMAC signing key, so unlike a bearer token it has
 * to be recoverable in plaintext to sign each delivery — it cannot be one-way
 * hashed. Cloudflare encrypts Durable Object storage at rest already; this
 * adds AES-256-GCM on top, keyed by a Worker secret binding, so a raw dump of
 * the database never yields the signing keys.
 *
 * The envelope is `enc:v1:<base64 iv>:<base64 ciphertext>`.
 *
 * One difference from the reference worth stating: its AES-GCM primitives are
 * marked no-cover, because they run only inside the Cloudflare Pyodide
 * runtime and cannot be exercised by its unit tests. `crypto.subtle` is
 * native both here and in a Worker, so the whole thing — including a real
 * round trip and a wrong-key rejection — is tested rather than only its
 * wiring.
 */

/** What an encrypted value begins with. */
const ENVELOPE_PREFIX = "enc:v1:";

/**
 * The initialisation vector length, in bytes.
 *
 * Twelve is what AES-GCM is specified around and what the reference writes.
 * The primitive accepts other lengths, so this is not a correctness
 * requirement here — it is the interoperability one: an envelope written by
 * either implementation has to be readable by the other.
 */
const IV_BYTES = 12;

/** A Worker environment that may carry the key binding. */
export interface WebhookCryptoEnv {
  WEBHOOK_SECRET_KEY?: unknown;
}

/** The base64 key from the binding, if one is configured. */
export function webhookKeyB64(env: WebhookCryptoEnv): string | undefined {
  const raw = env.WEBHOOK_SECRET_KEY;
  // Falsy bindings — absent, empty, `false`, zero — are no key. A blank
  // string is not a key either.
  if (raw === undefined || raw === null || raw === false || raw === "" || raw === 0) {
    return undefined;
  }
  const key = String(raw).trim();
  return key === "" ? undefined : key;
}

/** Whether a stored value carries the envelope, rather than being plaintext. */
export function isEncrypted(stored: string): boolean {
  return stored.startsWith(ENVELOPE_PREFIX);
}

/** Base64 for bytes. */
function toBase64(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64");
}

/**
 * Bytes for base64.
 *
 * @throws {Error} When the text is not base64.
 */
function fromBase64(text: string): Uint8Array<ArrayBuffer> {
  const bytes = Buffer.from(text, "base64");
  // Buffer is lenient where the reference's decoder is not: it drops what it
  // cannot read rather than refusing, and it accepts a value with its padding
  // missing. Re-encoding and comparing exactly is what makes both fail here
  // too, so an envelope this accepts is one the reference accepts.
  if (bytes.toString("base64") !== text) {
    throw new Error("not base64");
  }
  return new Uint8Array(bytes);
}

/**
 * The key handle `crypto.subtle` hands back.
 *
 * Named rather than imported: the Workers types and Node's own disagree on
 * where it lives, and nothing here needs more than to pass it along.
 */
type SubtleKey = Awaited<ReturnType<typeof crypto.subtle.importKey>>;

/** Import the raw key for one use. */
async function importKey(keyB64: string, usage: "encrypt" | "decrypt"): Promise<SubtleKey> {
  return crypto.subtle.importKey("raw", fromBase64(keyB64), { name: "AES-GCM" }, false, [usage]);
}

/**
 * Encrypt a signing secret for storage.
 *
 * With no key configured the secret is returned unchanged. That is the
 * single-tenant case: refusing to sign at all would be worse than storing in
 * the clear on storage that is already encrypted.
 */
export async function encryptSecret(env: WebhookCryptoEnv, plaintext: string): Promise<string> {
  const keyB64 = webhookKeyB64(env);
  if (keyB64 === undefined) {
    return plaintext;
  }
  const key = await importKey(keyB64, "encrypt");
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(plaintext));
  return `${ENVELOPE_PREFIX}${toBase64(iv)}:${toBase64(new Uint8Array(ciphertext))}`;
}

/**
 * Read a stored signing secret back.
 *
 * A value written before this existed is plaintext and is returned unchanged;
 * there is no migration step, so those rows have to keep working.
 *
 * Anything that cannot be decrypted returns nothing, and the delivery goes
 * unsigned. A signature that does not verify is worse than none at all: a
 * receiver checking signatures would reject it, while one that is not would
 * trust it.
 */
export async function decryptSecret(env: WebhookCryptoEnv, stored: string): Promise<string | undefined> {
  if (!isEncrypted(stored)) {
    return stored;
  }
  const keyB64 = webhookKeyB64(env);
  if (keyB64 === undefined) {
    // The catch below would swallow this too, but an envelope with no key is
    // a deployment that lost its key, not a value that failed to decrypt.
    return undefined;
  }
  // Split at most three times, so the ciphertext keeps anything of its own —
  // base64 has no colons, but a tampered envelope may.
  const parts = splitAtMost(stored, ":", 3);
  if (parts.length !== 4) {
    // Also caught by the decoding below; this says the envelope was the wrong
    // shape rather than its contents being unreadable.
    return undefined;
  }
  try {
    const iv = fromBase64(parts[2] as string);
    const ciphertext = fromBase64(parts[3] as string);
    const key = await importKey(keyB64, "decrypt");
    const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ciphertext);
    return new TextDecoder().decode(plaintext);
  } catch {
    // A corrupted ciphertext, the wrong key, or a key that is not one.
    return undefined;
  }
}

/** Split on a separator at most `limit` times, keeping the rest in the last field. */
function splitAtMost(text: string, separator: string, limit: number): string[] {
  const parts: string[] = [];
  let rest = text;
  for (let index = 0; index < limit; index += 1) {
    const at = rest.indexOf(separator);
    if (at === -1) {
      break;
    }
    parts.push(rest.slice(0, at));
    rest = rest.slice(at + separator.length);
  }
  parts.push(rest);
  return parts;
}
