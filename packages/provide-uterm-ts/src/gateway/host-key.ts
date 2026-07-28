//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The SSH server's host key.
 *
 * Port of the host-key half of `provide.uterm.transports.ssh` — loading one
 * from disk, refusing it if its permissions are wrong, and generating an
 * ed25519 key when there is none.
 *
 * The reference gets the encoding from asyncssh. There is no equivalent here:
 * Node generates ed25519 keys but emits them as PKCS#8, and an SSH server
 * wants the OpenSSH private-key format. So that format is written out below —
 * it is well defined, and the alternative is shelling out to `ssh-keygen`,
 * which would make a running server depend on a binary being installed.
 */

import { generateKeyPairSync } from "node:crypto";
import { chmodSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { HOST_KEY_MODE, InsecureHostKeyError, verifyKeyPermissions } from "./ssh-policy.ts";

/** What the file is called inside the key directory. */
export const HOST_KEY_FILENAME = "ssh_host_key";

/** The directory holding it, which is private to the server. */
export const HOST_KEY_DIR_MODE = 0o700;

/** The OpenSSH private-key container's magic string, NUL-terminated. */
const OPENSSH_MAGIC = Buffer.from("openssh-key-v1\0", "latin1");

/** The key type this generates and reads. */
const KEY_TYPE = "ssh-ed25519";

/** An ed25519 seed and public key are both this long. */
const ED25519_KEY_BYTES = 32;

/** The armour an OpenSSH private key is wrapped in. */
const PEM_HEADER = "-----BEGIN OPENSSH PRIVATE KEY-----"; // pragma: allowlist secret - a format marker, not a key
const PEM_FOOTER = "-----END OPENSSH PRIVATE KEY-----";

/** How many base64 characters go on one line of the armour. */
const PEM_LINE_LENGTH = 70;

/** Length-prefix a field, as the SSH wire format does. */
function sshString(value: Uint8Array | string): Buffer {
  const body = typeof value === "string" ? Buffer.from(value, "utf8") : Buffer.from(value);
  const length = Buffer.alloc(4);
  length.writeUInt32BE(body.length);
  return Buffer.concat([length, body]);
}

/**
 * Encode an ed25519 key pair as an OpenSSH private key.
 *
 * Unencrypted: a host key is read by the server at startup with nothing to
 * supply a passphrase, which is why the file's permissions carry the whole
 * weight of protecting it.
 */
export function encodeOpenSshEd25519(seed: Uint8Array, publicKey: Uint8Array, comment = ""): string {
  if (seed.length !== ED25519_KEY_BYTES || publicKey.length !== ED25519_KEY_BYTES) {
    throw new InsecureHostKeyError(
      `ed25519 key material must be ${ED25519_KEY_BYTES} bytes (got ${seed.length} and ${publicKey.length})`,
    );
  }
  const publicBlob = Buffer.concat([sshString(KEY_TYPE), sshString(publicKey)]);

  // The two check integers match on an unencrypted key; a reader compares
  // them to tell a wrong passphrase from a corrupt file.
  const check = Buffer.alloc(4);
  check.writeUInt32BE(0x7f7f7f7f);
  const body = Buffer.concat([
    check,
    check,
    sshString(KEY_TYPE),
    sshString(publicKey),
    // The private field is the seed followed by the public key, which is how
    // ed25519 signing keys are stored throughout SSH.
    sshString(Buffer.concat([seed, publicKey])),
    sshString(comment),
  ]);
  // Padded to the cipher's block size — eight even for `none`, and the bytes
  // count up from one so a reader can check them.
  const padding = Buffer.from(Array.from({ length: (8 - (body.length % 8)) % 8 }, (_value, index) => index + 1));

  const container = Buffer.concat([
    OPENSSH_MAGIC,
    sshString("none"),
    sshString("none"),
    sshString(""),
    Buffer.from([0, 0, 0, 1]),
    sshString(publicBlob),
    sshString(Buffer.concat([body, padding])),
  ]);

  // Always matches: the container carries a fixed header before anything
  // else, so the encoding is never empty.
  const encoded = container.toString("base64").match(new RegExp(`.{1,${PEM_LINE_LENGTH}}`, "g")) as RegExpMatchArray;
  return [PEM_HEADER, ...encoded, PEM_FOOTER, ""].join("\n");
}

/** Generate a fresh ed25519 host key in OpenSSH format. */
export function generateHostKey(comment = "provide-uterm"): string {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519", {
    privateKeyEncoding: { type: "pkcs8", format: "der" },
    publicKeyEncoding: { type: "spki", format: "der" },
  });
  // Both encodings end with the raw key material, and ed25519 has exactly one
  // size — so the last 32 bytes are the seed and the public key respectively.
  // Parsing the surrounding ASN.1 would buy nothing here.
  return encodeOpenSshEd25519(
    privateKey.subarray(privateKey.length - ED25519_KEY_BYTES),
    publicKey.subarray(publicKey.length - ED25519_KEY_BYTES),
    comment,
  );
}

/** How the key store reads the filesystem, so a test can watch it. */
export interface HostKeyStoreOptions {
  /** The current user, for the ownership check. Omitted where there is none. */
  currentUid?: number;
  /** Called when an existing key could not be read and is being replaced. */
  onRegenerate?: (reason: string) => void;
}

/**
 * Load the host key from `directory`, or generate and save one.
 *
 * A key whose permissions are wrong is refused outright rather than replaced:
 * a world-readable host key may already have been copied, and quietly
 * generating a new one would hide that from the operator who needs to know.
 *
 * A key that cannot be *parsed* is a different matter — that is a corrupt
 * file, not a leaked one, and it is replaced so the server can start.
 *
 * @throws {InsecureHostKeyError} When an existing key's mode or owner is
 *   wrong.
 */
export function getOrCreateHostKey(directory: string, options: HostKeyStoreOptions = {}): string {
  const path = join(directory, HOST_KEY_FILENAME);
  let existing: Buffer | undefined;
  try {
    const stat = statSync(path);
    // Checked before the file is read: the point is to notice that it is
    // exposed, not to use it and mention the exposure afterwards.
    verifyKeyPermissions(path, { mode: stat.mode & 0o777, uid: stat.uid }, options.currentUid);
    existing = readFileSync(path);
  } catch (error) {
    if (error instanceof InsecureHostKeyError) {
      throw error;
    }
    // No key yet, which is the ordinary first start.
  }

  if (existing !== undefined) {
    if (existing.includes(PEM_HEADER)) {
      return existing.toString("utf8");
    }
    options.onRegenerate?.(`unreadable host key at ${path}`);
  }

  const key = generateHostKey();
  try {
    mkdirSync(directory, { recursive: true });
    // Tightened so the directory is private even if it already existed with
    // wider permissions.
    chmodSync(directory, HOST_KEY_DIR_MODE);
    // Both, and neither is redundant. The mode passed to `writeFileSync`
    // closes the window in which a newly created file would be readable
    // before the `chmod` lands — but it applies only to a file being
    // created, and is masked by the process umask. The `chmod` covers the
    // rest. Under this test's umask, and with any pre-existing key already
    // refused by the permission check above, neither alone is observable.
    writeFileSync(path, key, { mode: HOST_KEY_MODE });
    chmodSync(path, HOST_KEY_MODE);
  } catch (error) {
    // A server that cannot persist its key still starts, with a key that
    // lasts as long as the process — noisy for clients, but running.
    options.onRegenerate?.(`could not save host key: ${(error as Error).message}`);
  }
  return key;
}
