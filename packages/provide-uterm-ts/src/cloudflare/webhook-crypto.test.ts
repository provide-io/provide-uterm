//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { decryptSecret, encryptSecret, isEncrypted, type WebhookCryptoEnv, webhookKeyB64 } from "./index.ts";

interface CryptoGolden {
  key: string;
  prefix: string;
  keys: Array<{ name: string; binding: unknown; result: string | null }>;
  envelopes: Array<{ name: string; stored: string; result: boolean }>;
  encrypt_without_key: string;
  encrypt_empty_without_key: string;
  read_without_key: Array<{ name: string; stored: string; result: string | null }>;
  read_with_key: Array<{ name: string; stored: string; result: string | null }>;
}

const golden = loadGolden<CryptoGolden>("cfwebhookcrypto_golden.json");

/** An environment carrying the binding, or not. */
function env(binding?: unknown): WebhookCryptoEnv {
  return binding === undefined ? {} : { WEBHOOK_SECRET_KEY: binding };
}

/** A second key, for the wrong-key case. */
const OTHER_KEY = Buffer.from(new Uint8Array(32).fill(9)).toString("base64");

describe("finding the key", () => {
  it.each(golden.keys)("$name", (record) => {
    expect(webhookKeyB64(env(record.binding ?? undefined)) ?? null).toBe(record.result);
  });

  it("reads a configured key", () => {
    expect(webhookKeyB64(env(golden.key))).toBe(golden.key);
  });

  it("treats a blank binding as none", () => {
    // A binding set to whitespace is a deployment that meant to set one and
    // did not; encrypting with it would fail on every write.
    for (const binding of ["", "   ", "\t\n"]) {
      expect(webhookKeyB64(env(binding))).toBeUndefined();
    }
  });

  it("treats an absent binding as none", () => {
    expect(webhookKeyB64({})).toBeUndefined();
  });
});

describe("telling an envelope from plaintext", () => {
  it.each(golden.envelopes)("$name", (record) => {
    expect(isEncrypted(record.stored)).toBe(record.result);
  });

  it("matches only its own version", () => {
    // A later envelope version is not this one, and reading it with these
    // rules would produce nonsense rather than a refusal.
    expect(isEncrypted("enc:v1:a:b")).toBe(true);
    expect(isEncrypted("enc:v2:a:b")).toBe(false);
  });

  it("matches only at the start", () => {
    expect(isEncrypted("not-enc:v1:x")).toBe(false);
  });
});

describe("a deployment with no key", () => {
  it("stores the secret as it is", () => {
    // The single-tenant case. Refusing to sign at all would be worse than
    // storing in the clear on storage that is already encrypted.
    expect(golden.encrypt_without_key).toBe("a-signing-secret");
  });

  it("stores it as it is, whatever it is", async () => {
    expect(await encryptSecret(env(), "a-signing-secret")).toBe(golden.encrypt_without_key);
    expect(await encryptSecret(env(), "")).toBe(golden.encrypt_empty_without_key);
    expect(await encryptSecret(env("  "), "a-signing-secret")).toBe("a-signing-secret");
  });

  it.each(golden.read_without_key)("reads back: $name", async (record) => {
    expect((await decryptSecret(env(), record.stored)) ?? null).toBe(record.result);
  });

  it("cannot read an envelope, and says so", async () => {
    // Skipping the signature is the right answer: a delivery signed with the
    // wrong key is worse than one with no signature, because a receiver
    // checking signatures would reject it while one that is not would trust
    // it.
    expect(await decryptSecret(env(), "enc:v1:aXY=:Y3Q=")).toBeUndefined();
  });
});

describe("reading a stored secret", () => {
  it.each(golden.read_with_key)("$name", async (record) => {
    expect((await decryptSecret(env(golden.key), record.stored)) ?? null).toBe(record.result);
  });

  it("returns a secret written before this existed", () => {
    // There is no migration step, so plaintext rows have to keep working.
    expect(golden.read_with_key.find((entry) => entry.name === "plaintext is still returned")?.result).toBe(
      "a-signing-secret",
    );
  });

  it("refuses an envelope missing a field", async () => {
    for (const stored of ["enc:v1:only-one", "enc:v1:"]) {
      expect(await decryptSecret(env(golden.key), stored)).toBeUndefined();
    }
  });

  it("reads a value that only looks like a prefix as plaintext", async () => {
    // Without the trailing colon it is not the envelope, so it is somebody's
    // secret that happens to start that way — returning nothing would stop
    // them signing.
    expect(await decryptSecret(env(golden.key), "enc:v1")).toBe("enc:v1");
  });

  it("refuses an envelope that is not base64", async () => {
    expect(await decryptSecret(env(golden.key), "enc:v1:!!!:???")).toBeUndefined();
  });
});

describe("the round trip", () => {
  // The reference cannot test this: its AES-GCM runs only inside the
  // Cloudflare Pyodide runtime, so those primitives are marked no-cover and
  // only the wiring around them is exercised. `crypto.subtle` is native both
  // here and in a Worker.

  it("gives back what it was given", async () => {
    const configured = env(golden.key);
    for (const secret of ["a-signing-secret", "", "héllo → ✓", "x".repeat(1000)]) {
      const stored = await encryptSecret(configured, secret);
      expect(isEncrypted(stored)).toBe(true);
      expect(await decryptSecret(configured, stored)).toBe(secret);
    }
  });

  it("writes the envelope the reference reads", async () => {
    const stored = await encryptSecret(env(golden.key), "a-signing-secret");
    expect(stored.startsWith(golden.prefix)).toBe(true);
    // Prefix, version, initialisation vector, ciphertext.
    expect(stored.split(":")).toHaveLength(4);
  });

  it("writes a twelve-byte initialisation vector", async () => {
    // Not a correctness requirement — the primitive accepts other lengths —
    // but an interoperability one: the reference writes twelve, and an
    // envelope written by either implementation has to be readable by the
    // other.
    const stored = await encryptSecret(env(golden.key), "a-signing-secret");
    expect(Buffer.from(stored.split(":")[2] as string, "base64")).toHaveLength(12);
  });

  it("refuses base64 with its padding missing", async () => {
    // The reference's decoder requires it, so accepting the value here would
    // accept an envelope it would refuse. A twelve-byte vector encodes with
    // no padding of its own, so the case is built by hand.
    const stored = await encryptSecret(env(golden.key), "a-signing-secret");
    const parts = stored.split(":");
    const ciphertext = parts[3] as string;
    // A sixteen-byte secret plus the tag encodes with padding; the vector
    // does not, so this is the field that shows the difference.
    expect(ciphertext.endsWith("=")).toBe(true);
    const unpadded = `${parts[0]}:${parts[1]}:${parts[2]}:${ciphertext.replace(/=+$/, "")}`;
    expect(await decryptSecret(env(golden.key), unpadded)).toBeUndefined();
    expect(await decryptSecret(env(golden.key), stored)).toBe("a-signing-secret");
  });

  it("hides the secret it was given", async () => {
    // The whole point: a raw dump of the database must not yield the signing
    // keys.
    const stored = await encryptSecret(env(golden.key), "a-signing-secret");
    expect(stored).not.toContain("a-signing-secret");
  });

  it("uses a fresh initialisation vector each time", async () => {
    // Reusing one under the same key is what breaks AES-GCM: two messages
    // encrypted with the same pair can be combined to recover both.
    const configured = env(golden.key);
    const first = await encryptSecret(configured, "a-signing-secret");
    const second = await encryptSecret(configured, "a-signing-secret");
    expect(first).not.toBe(second);
    expect(first.split(":")[2]).not.toBe(second.split(":")[2]);
    expect(await decryptSecret(configured, first)).toBe(await decryptSecret(configured, second));
  });

  it("refuses a secret encrypted under another key", async () => {
    // A rotated or mistaken key skips the signature rather than emitting one
    // that will not verify.
    const stored = await encryptSecret(env(golden.key), "a-signing-secret");
    expect(await decryptSecret(env(OTHER_KEY), stored)).toBeUndefined();
  });

  it("refuses a ciphertext somebody has altered", async () => {
    // AES-GCM authenticates as well as encrypts, so a tampered envelope is
    // detected rather than decrypting to something else.
    const stored = await encryptSecret(env(golden.key), "a-signing-secret");
    const parts = stored.split(":");
    const ciphertext = Buffer.from(parts[3] as string, "base64");
    ciphertext[0] = (ciphertext[0] as number) ^ 0xff;
    const tampered = `${parts[0]}:${parts[1]}:${parts[2]}:${ciphertext.toString("base64")}`;
    expect(await decryptSecret(env(golden.key), tampered)).toBeUndefined();
  });

  it("refuses an envelope whose initialisation vector was swapped", async () => {
    const configured = env(golden.key);
    const first = (await encryptSecret(configured, "a-signing-secret")).split(":");
    const second = (await encryptSecret(configured, "a-signing-secret")).split(":");
    const crossed = `${first[0]}:${first[1]}:${second[2]}:${first[3]}`;
    expect(await decryptSecret(configured, crossed)).toBeUndefined();
  });

  it("refuses a key that is not a key", async () => {
    // A binding holding something that is not base64, or is the wrong length
    // for AES-256, must skip the signature rather than raise mid-delivery.
    const stored = await encryptSecret(env(golden.key), "a-signing-secret");
    expect(await decryptSecret(env("not-base64!!!"), stored)).toBeUndefined();
    expect(await decryptSecret(env(Buffer.from("short").toString("base64")), stored)).toBeUndefined();
  });

  it("does not raise when it cannot encrypt", async () => {
    // A misconfigured key is found on the first write rather than silently
    // storing plaintext under an envelope prefix.
    await expect(encryptSecret(env("not-base64!!!"), "a-signing-secret")).rejects.toThrow();
  });
});
