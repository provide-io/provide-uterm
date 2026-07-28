//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  AuthorizedKeysFileResolver,
  fingerprintFromOpensshBlob,
  NullResolver,
  parseAuthorizedKeysLine,
} from "./index.ts";

interface AuthGolden {
  payloads: Record<string, string>;
  blobs: Array<{ name: string; blob: number[]; ok: boolean; fingerprint?: string; error?: string }>;
  bad_blobs: Array<{ name: string; blob: number[]; ok: boolean; error?: string }>;
  lines: Array<{
    name: string;
    line: string;
    ok: boolean;
    fingerprint?: string;
    subject?: string;
    claims?: Record<string, unknown>;
  }>;
  bad_lines: Array<{ name: string; line: string; ok: boolean; error?: string }>;
  resolver: {
    file_lines: string[];
    alice_fingerprint: string;
    bob_fingerprint: string;
    unknown_fingerprint: string;
    revoked_fingerprint: string;
    hits: Record<string, { subject: string; claims: Record<string, unknown>; fingerprint: string } | null>;
  };
  null_resolver: null;
  fingerprint_prefix: string;
}

const golden = loadGolden<AuthGolden>("auth_golden.json");

const directory = mkdtempSync(join(tmpdir(), "uterm-auth-"));
afterAll(() => rmSync(directory, { recursive: true, force: true }));

/** Write an authorized_keys file and return its path. */
function keysFile(lines: string[], name = "authorized_keys"): string {
  const path = join(directory, name);
  writeFileSync(path, `${lines.join("\n")}\n`, "utf8");
  return path;
}

describe("fingerprintFromOpensshBlob", () => {
  it.each(golden.blobs)("$name", (record) => {
    // The fingerprint is the whole basis of the match. Fingerprint the wrong
    // bytes and every key stops resolving; worse, two keys could collide into
    // one identity.
    expect(fingerprintFromOpensshBlob(Uint8Array.from(record.blob))).toBe(record.fingerprint);
  });

  it.each(golden.bad_blobs)("refuses $name", (record) => {
    expect(() => fingerprintFromOpensshBlob(Uint8Array.from(record.blob))).toThrow(record.error as string);
  });

  it("prints what ssh-keygen prints", () => {
    // Padded base64, or hex, or a different prefix, would all be a fingerprint
    // nobody can paste from `ssh-keygen -lf`.
    const record = golden.blobs.find((entry) => entry.name === "openssh text");
    expect(record?.fingerprint?.startsWith(golden.fingerprint_prefix)).toBe(true);
    expect(record?.fingerprint).not.toContain("=");
  });

  it("ignores the comment", () => {
    // The same key with two comments is the same key.
    const bare = golden.blobs.find((entry) => entry.name === "openssh text");
    const commented = golden.blobs.find((entry) => entry.name === "openssh text with a comment");
    expect(commented?.fingerprint).toBe(bare?.fingerprint);
  });

  it("ignores surrounding whitespace", () => {
    // Leading whitespace matters most: without the strip the key type no
    // longer starts the string, and the *text* gets fingerprinted instead of
    // the key — so the same key resolves to nothing.
    const bare = golden.blobs.find((entry) => entry.name === "openssh text");
    for (const name of ["openssh text with trailing space", "openssh text with leading space"]) {
      const spaced = golden.blobs.find((entry) => entry.name === name);
      expect(spaced?.fingerprint).toBe(bare?.fingerprint);
    }
  });

  it("gives the text and binary forms of one key the same fingerprint", () => {
    // A resolver is handed either, depending on the caller.
    const text = golden.blobs.find((entry) => entry.name === "openssh text");
    const binary = golden.blobs.find((entry) => entry.name === "binary wire format");
    expect(binary?.fingerprint).toBe(text?.fingerprint);
  });

  it("gives different keys different fingerprints", () => {
    const seen = golden.blobs
      .filter((entry) => ["openssh text", "rsa", "ecdsa", "security key"].includes(entry.name))
      .map((entry) => entry.fingerprint);
    expect(new Set(seen).size).toBe(seen.length);
  });

  it("accepts a string as well as bytes", () => {
    const record = golden.blobs.find((entry) => entry.name === "openssh text");
    expect(fingerprintFromOpensshBlob(`ssh-ed25519 ${golden.payloads.ed25519}`)).toBe(record?.fingerprint);
  });

  it("recognises every key-type prefix", () => {
    // A prefix it does not know is treated as raw wire bytes, and fingerprints
    // the *text* rather than the key.
    for (const name of ["openssh text", "rsa", "ecdsa", "security key"]) {
      const record = golden.blobs.find((entry) => entry.name === name);
      expect(record?.ok).toBe(true);
    }
  });
});

describe("parseAuthorizedKeysLine", () => {
  it.each(golden.lines)("$name", (record) => {
    const entry = parseAuthorizedKeysLine(record.line);
    expect(entry.fingerprint).toBe(record.fingerprint);
    expect(entry.subject).toBe(record.subject);
    expect(entry.claims).toStrictEqual(record.claims);
  });

  it.each(golden.bad_lines)("refuses $name", (record) => {
    expect(() => parseAuthorizedKeysLine(record.line)).toThrow(record.error as string);
  });

  it("falls back from the subject option to the comment to the key", () => {
    // Every key gets *some* subject, so a line without one is still usable
    // rather than silently unresolvable.
    const explicit = golden.lines.find((entry) => entry.name === "explicit subject");
    const commented = golden.lines.find((entry) => entry.name === "key with a comment");
    const bare = golden.lines.find((entry) => entry.name === "bare key");
    expect(explicit?.subject).toBe("sre:alice");
    expect(commented?.subject).toBe("alice@laptop");
    expect(bare?.subject).toBe(`key:${bare?.fingerprint}`);
  });

  it("treats an empty subject option as absent", () => {
    const record = golden.lines.find((entry) => entry.name === "empty subject falls back");
    expect(record?.subject).toBe("alice@laptop");
  });

  it("keeps the whole comment, spaces and all", () => {
    const record = golden.lines.find((entry) => entry.name === "comment with spaces");
    expect(record?.subject).toBe("alice on her laptop");
  });

  it("strips the claim- prefix and keeps the rest", () => {
    const record = golden.lines.find((entry) => entry.name === "several claims");
    expect(record?.claims).toStrictEqual({ role: "oncall", display: "alice" });
  });

  it("puts an option it does not recognise under _options", () => {
    // Preserved rather than dropped, so a consumer can still see `no-pty`.
    const record = golden.lines.find((entry) => entry.name === "an unrecognised option");
    expect(record?.claims).toStrictEqual({ _options: { "no-pty": true } });
  });

  it("does not add _options when there are none", () => {
    const record = golden.lines.find((entry) => entry.name === "one claim");
    expect(record?.claims).toStrictEqual({ role: "oncall" });
  });

  it("keeps a quoted option together across spaces and commas", () => {
    // The options field ends at the first whitespace *outside* quotes.
    // Splitting inside one would read `no-pty` as the key type and the line
    // would be refused — locking that key out.
    const record = golden.lines.find((entry) => entry.name === "a quoted option containing a space and a comma");
    expect(record?.claims).toStrictEqual({ _options: { command: "echo hi, there", "no-pty": true } });
    expect(record?.subject).toBe("alice@laptop");
  });

  it("keeps everything after the first = in an unquoted value", () => {
    const record = golden.lines.find((entry) => entry.name === "an unquoted option value");
    expect(record?.claims).toStrictEqual({ _options: { environment: "FOO=bar" } });
  });

  it("lets the last of a repeated option win", () => {
    const record = golden.lines.find((entry) => entry.name === "a repeated option");
    expect(record?.claims).toStrictEqual({ role: "second" });
  });

  it("skips an empty option between commas", () => {
    const record = golden.lines.find((entry) => entry.name === "an empty option between commas");
    expect(record?.claims).toStrictEqual({ _options: { "no-pty": true }, role: "oncall" });
  });

  it("tolerates an options field that ends in a comma", () => {
    // A trailing comma leaves nothing after it; treating that as a flag
    // would put an empty key in _options.
    const record = golden.lines.find((entry) => entry.name === "options ending in a comma");
    expect(record?.claims).toStrictEqual({ _options: { "no-pty": true } });
  });

  it("tolerates extra whitespace after the options", () => {
    const record = golden.lines.find((entry) => entry.name === "extra whitespace after the options");
    expect(record?.ok).toBe(true);
  });

  it("gives a line with options the same fingerprint as one without", () => {
    // The options are not part of the key.
    const bare = golden.lines.find((entry) => entry.name === "bare key");
    const optioned = golden.lines.find((entry) => entry.name === "an unrecognised option");
    expect(optioned?.fingerprint).toBe(bare?.fingerprint);
  });
});

describe("NullResolver", () => {
  it("never resolves anything", async () => {
    // It exists so a caller can always pass a resolver; resolving would be
    // the opposite of what it is for.
    const resolver = new NullResolver();
    const resolved = await resolver.resolve("SHA256:anything", { pubkeyBlob: new Uint8Array(), username: "root" });
    expect(resolved).toBe(golden.null_resolver ?? undefined);
    expect(resolved).toBeUndefined();
  });
});

describe("AuthorizedKeysFileResolver", () => {
  const path = keysFile(golden.resolver.file_lines);

  it("resolves a key in the file", async () => {
    const resolver = new AuthorizedKeysFileResolver(path);
    const resolved = await resolver.resolve(golden.resolver.alice_fingerprint, {
      pubkeyBlob: new Uint8Array(),
      username: "",
    });
    expect(resolved).toStrictEqual(golden.resolver.hits.alice);
  });

  it("stamps the fingerprint it matched onto the identity", async () => {
    // The resolver may leave it empty; the caller needs to know which key
    // actually let this identity in.
    expect(golden.resolver.hits.alice?.fingerprint).toBe(golden.resolver.alice_fingerprint);
  });

  it("resolves a second key in the same file", async () => {
    const resolver = new AuthorizedKeysFileResolver(path);
    const resolved = await resolver.resolve(golden.resolver.bob_fingerprint, {
      pubkeyBlob: new Uint8Array(),
      username: "",
    });
    expect(resolved).toStrictEqual(golden.resolver.hits.bob);
  });

  it("does not resolve a key that is not in the file", async () => {
    const resolver = new AuthorizedKeysFileResolver(path);
    expect(
      await resolver.resolve(golden.resolver.unknown_fingerprint, {
        pubkeyBlob: new Uint8Array(),
        username: "",
      }),
    ).toBeUndefined();
  });

  it("ignores the username", async () => {
    // This resolver matches on the key alone; the username is offered for
    // resolvers that want it.
    const resolver = new AuthorizedKeysFileResolver(path);
    const resolved = await resolver.resolve(golden.resolver.alice_fingerprint, {
      pubkeyBlob: new Uint8Array(),
      username: "someone-else",
    });
    expect(resolved).toStrictEqual(golden.resolver.hits.alice_other_username);
  });

  it("keeps reading past a line it cannot parse", async () => {
    // One bad line must not lock everybody out, so the file is read entry by
    // entry rather than all or nothing — and bob's key is *after* the bad
    // line, so an abort would lose it.
    expect(golden.resolver.file_lines).toContain("ssh-ed25519");
    expect(golden.resolver.file_lines.indexOf("ssh-ed25519")).toBeLessThan(
      golden.resolver.file_lines.findIndex((line) => line.includes("bob@desktop")),
    );
    const resolver = new AuthorizedKeysFileResolver(path);
    expect(
      await resolver.resolve(golden.resolver.bob_fingerprint, { pubkeyBlob: new Uint8Array(), username: "" }),
    ).not.toBeUndefined();
  });

  it("does not resolve a key that has been commented out", async () => {
    // Commenting a key out is how it is revoked. Parsing comment lines would
    // let a revoked key straight back in.
    expect(golden.resolver.file_lines.some((line) => line.startsWith("# ssh-"))).toBe(true);
    const resolver = new AuthorizedKeysFileResolver(path);
    expect(
      await resolver.resolve(golden.resolver.revoked_fingerprint, { pubkeyBlob: new Uint8Array(), username: "" }),
    ).toBe(golden.resolver.hits.revoked ?? undefined);
  });

  it("treats a missing file as an empty one", async () => {
    // A gateway that has not been given a key file yet should refuse keys,
    // not fail to start.
    const resolver = new AuthorizedKeysFileResolver(join(directory, "does-not-exist"));
    expect(
      await resolver.resolve(golden.resolver.alice_fingerprint, { pubkeyBlob: new Uint8Array(), username: "" }),
    ).toBe(golden.resolver.hits.missing_file ?? undefined);
  });

  it("picks up a key added after it was constructed", async () => {
    // Parsed on each call, so a rotation takes effect immediately rather
    // than at the next restart.
    const rotating = keysFile(["# empty for now"], "rotating");
    const resolver = new AuthorizedKeysFileResolver(rotating);
    expect(
      await resolver.resolve(golden.resolver.alice_fingerprint, { pubkeyBlob: new Uint8Array(), username: "" }),
    ).toBeUndefined();
    writeFileSync(rotating, `${golden.resolver.file_lines[3]}\n`, "utf8");
    expect(
      await resolver.resolve(golden.resolver.alice_fingerprint, { pubkeyBlob: new Uint8Array(), username: "" }),
    ).toStrictEqual(golden.resolver.hits.alice);
  });

  it("stops looking once it has a match", async () => {
    // Two lines for one key is a misconfiguration; the first wins rather
    // than the last, so the file reads top down like OpenSSH's own.
    const duplicated = keysFile(
      [
        `subject="first" ssh-ed25519 ${golden.payloads.ed25519}`,
        `subject="second" ssh-ed25519 ${golden.payloads.ed25519}`,
      ],
      "duplicated",
    );
    const resolver = new AuthorizedKeysFileResolver(duplicated);
    const resolved = await resolver.resolve(golden.resolver.alice_fingerprint, {
      pubkeyBlob: new Uint8Array(),
      username: "",
    });
    expect(resolved?.subject).toBe("first");
  });
});
