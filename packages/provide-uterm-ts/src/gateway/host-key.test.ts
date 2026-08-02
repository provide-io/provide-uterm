//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { chmodSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { utils as sshUtils } from "ssh2";
import { afterAll, describe, expect, it } from "vitest";
import {
  encodeOpenSshEd25519,
  generateHostKey,
  getOrCreateHostKey,
  HOST_KEY_DIR_MODE,
  HOST_KEY_FILENAME,
  HOST_KEY_MODE,
  InsecureHostKeyError,
} from "./index.ts";

const scratch = mkdtempSync(join(tmpdir(), "uterm-hostkey-"));
afterAll(() => rmSync(scratch, { recursive: true, force: true }));

/** A key with a chosen comment, to vary the body's length. */
function generateHostKeyWithComment(comment: string): string {
  return generateHostKey(comment);
}

/** A fresh key directory for one test. */
function keyDir(name: string): string {
  return join(scratch, name);
}

describe("generating a host key", () => {
  it("produces a key the SSH library accepts", () => {
    // The only test that matters: Node emits PKCS#8 and an SSH server wants
    // the OpenSSH format, so this encoding is written by hand and has to be
    // right down to the byte.
    const parsed = sshUtils.parseKey(generateHostKey());
    expect(parsed).not.toBeInstanceOf(Error);
    expect((parsed as { type: string }).type).toBe("ssh-ed25519");
  });

  it("produces a key that actually signs", () => {
    // A key that parses but cannot sign would fail at the first handshake.
    const parsed = sshUtils.parseKey(generateHostKey()) as {
      sign(data: Buffer): Buffer;
      verify(data: Buffer, signature: Buffer): boolean;
    };
    const message = Buffer.from("the quick brown fox");
    expect(parsed.verify(message, parsed.sign(message))).toBe(true);
  });

  it("produces a different key each time", () => {
    expect(generateHostKey()).not.toBe(generateHostKey());
  });

  it("wraps the key in the armour a reader looks for", () => {
    const key = generateHostKey();
    expect(key.startsWith("-----BEGIN OPENSSH PRIVATE KEY-----\n")).toBe(true); // pragma: allowlist secret
    expect(key.trimEnd().endsWith("-----END OPENSSH PRIVATE KEY-----")).toBe(true);
  });

  it("carries the comment it was given", () => {
    const parsed = sshUtils.parseKey(generateHostKey()) as { comment: string };
    expect(parsed.comment).toBe("provide-uterm");
  });

  it("pads the body to the block size whatever the comment", () => {
    // The padding is a function of the comment's length, so a key with the
    // default comment happens to need none. A reader checks the padding
    // bytes count up from one, so getting this wrong fails only for some
    // comments — which is exactly the kind of bug that ships.
    for (const comment of ["", "x", "xx", "a-longer-comment", "provide-uterm"]) {
      const parsed = sshUtils.parseKey(generateHostKeyWithComment(comment));
      expect(parsed).not.toBeInstanceOf(Error);
      expect((parsed as { comment: string }).comment).toBe(comment);
    }
  });

  it("writes the container the format specifies", () => {
    // ssh2 reads the key without checking the padding, so nothing above
    // would notice if it were wrong — but OpenSSH itself does check, and a
    // host key this server writes has to be readable by the tools an
    // operator already has.
    const body = Buffer.from(
      generateHostKey("x")
        .split("\n")
        .filter((line) => !line.startsWith("-----"))
        .join(""),
      "base64",
    );

    // The private section is the last length-prefixed field.
    let offset = "openssh-key-v1\0".length;
    const readField = (): Buffer => {
      const length = body.readUInt32BE(offset);
      const field = body.subarray(offset + 4, offset + 4 + length);
      offset += 4 + length;
      return field;
    };
    readField(); // cipher
    readField(); // kdf
    readField(); // kdf options
    expect(body.readUInt32BE(offset)).toBe(1); // exactly one key
    offset += 4;
    readField(); // public blob
    const section = readField();

    // Padded to the cipher's block size, with the bytes counting up from one.
    expect(section.length % 8).toBe(0);
    const comment = "x";
    const unpadded = 4 + 4 + (4 + 11) + (4 + 32) + (4 + 64) + (4 + comment.length);
    const padding = [...section.subarray(unpadded)];
    expect(padding).toEqual(Array.from({ length: section.length - unpadded }, (_value, index) => index + 1));
    expect(padding.length).toBeGreaterThan(0);
    // Padded *to* the block size, not past it. A larger block would still
    // satisfy a reader, since any multiple of eight divides evenly — which
    // is exactly why the minimum has to be asserted rather than inferred.
    expect(padding.length).toBeLessThan(8);
  });

  it("refuses key material of the wrong size", () => {
    // Silently padding or truncating would produce a key that parses and
    // verifies nothing.
    const short = new Uint8Array(31);
    const right = new Uint8Array(32);
    expect(() => encodeOpenSshEd25519(short, right)).toThrow(InsecureHostKeyError);
    expect(() => encodeOpenSshEd25519(right, short)).toThrow(InsecureHostKeyError);
  });
});

describe("the host key store", () => {
  it("generates one on a first start", () => {
    const directory = keyDir("first");
    const key = getOrCreateHostKey(directory);
    expect(sshUtils.parseKey(key)).not.toBeInstanceOf(Error);
    expect(readFileSync(join(directory, HOST_KEY_FILENAME), "utf8")).toBe(key);
  });

  it("writes the file the reference writes", () => {
    // An operator may already have deployed a key at this path, and a server
    // that looked elsewhere would generate a second identity.
    expect(HOST_KEY_FILENAME).toBe("ssh_host_key");
  });

  it("keeps the same key across restarts", () => {
    // A host key that changed on every start would make every client warn
    // about a changed identity.
    const directory = keyDir("stable");
    expect(getOrCreateHostKey(directory)).toBe(getOrCreateHostKey(directory));
  });

  it("writes the key private and the directory private", () => {
    const directory = keyDir("modes");
    getOrCreateHostKey(directory);
    expect(statSync(join(directory, HOST_KEY_FILENAME)).mode & 0o777).toBe(HOST_KEY_MODE);
    expect(statSync(directory).mode & 0o777).toBe(HOST_KEY_DIR_MODE);
  });

  it("refuses a key anyone can read", () => {
    // Refused rather than replaced: a world-readable host key may already
    // have been copied, and quietly generating a new one would hide that
    // from the operator who needs to know.
    const directory = keyDir("exposed");
    getOrCreateHostKey(directory);
    chmodSync(join(directory, HOST_KEY_FILENAME), 0o644);
    expect(() => getOrCreateHostKey(directory)).toThrow(InsecureHostKeyError);
  });

  it("refuses a key wider than it should be even by one bit", () => {
    const directory = keyDir("group");
    getOrCreateHostKey(directory);
    chmodSync(join(directory, HOST_KEY_FILENAME), 0o660);
    expect(() => getOrCreateHostKey(directory)).toThrow(/insecure mode/);
  });

  it("replaces a key it cannot read", () => {
    // A corrupt file is not a leaked one: the server should start.
    const directory = keyDir("corrupt");
    getOrCreateHostKey(directory);
    writeFileSync(join(directory, HOST_KEY_FILENAME), "not a key at all", { mode: HOST_KEY_MODE });
    const reasons: string[] = [];
    const key = getOrCreateHostKey(directory, { onRegenerate: (reason) => reasons.push(reason) });
    expect(sshUtils.parseKey(key)).not.toBeInstanceOf(Error);
    expect(reasons[0]).toContain("unreadable host key");
  });

  it("starts with a temporary key when it cannot save one", () => {
    // Noisy for clients, but running beats refusing to start.
    //
    // The unwritable location is a regular file used as a parent directory, so
    // mkdir fails with ENOTDIR promptly on every platform. It used to be
    // "/proc/nonexistent/uterm", which hangs forever on Linux: node's recursive
    // mkdir creates the parent and retries the child, and procfs answers ENOENT
    // for a mkdir inside an existing directory, so the retry never terminates.
    // mkdirSync is synchronous, so vitest cannot time it out -- the file never
    // reported and the whole job sat until its timeout. macOS has no /proc, so
    // it passed locally and only ever hung in CI.
    const blocker = join(scratch, "not-a-directory");
    writeFileSync(blocker, "a regular file, so mkdir below it is ENOTDIR");
    const reasons: string[] = [];
    const key = getOrCreateHostKey(join(blocker, "uterm"), { onRegenerate: (reason) => reasons.push(reason) });
    expect(sshUtils.parseKey(key)).not.toBeInstanceOf(Error);
    expect(reasons.some((reason) => reason.includes("could not save"))).toBe(true);
  });

  it("checks the owner when the platform has one", () => {
    const directory = keyDir("owner");
    getOrCreateHostKey(directory);
    const uid = statSync(join(directory, HOST_KEY_FILENAME)).uid;
    expect(() => getOrCreateHostKey(directory, { currentUid: uid })).not.toThrow();
    expect(() => getOrCreateHostKey(directory, { currentUid: uid + 1 })).toThrow(/owned by uid/);
  });
});
