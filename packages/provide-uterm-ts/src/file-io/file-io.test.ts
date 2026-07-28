//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { closeSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { DEFAULT_PALETTE } from "../ansi/index.ts";
import { loadAns, loadPalette, loadTxt, secureCreate, secureOpenAppend } from "./index.ts";

let workDir: string;

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "uterm-file-io-"));
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
});

/** The permission bits of a path, as an octal number. */
function mode(path: string): number {
  return statSync(path).mode & 0o777;
}

describe("secureCreate", () => {
  it("creates the file with owner-only permissions", () => {
    const target = join(workDir, "sink.log");
    closeSync(secureCreate(target));
    expect(mode(target)).toBe(0o600);
  });

  it("creates missing parent directories with owner-only permissions", () => {
    const target = join(workDir, "nested", "deep", "sink.log");
    closeSync(secureCreate(target));
    expect(mode(join(workDir, "nested", "deep"))).toBe(0o700);
  });

  it("re-tightens a pre-existing loose parent directory", () => {
    // mkdir's mode argument only applies on creation, so a directory that
    // already exists keeps its old bits unless they are re-applied.
    const parent = join(workDir, "loose");
    mkdirSync(parent, { mode: 0o755 });
    closeSync(secureCreate(join(parent, "sink.log")));
    expect(mode(parent)).toBe(0o700);
  });

  it("re-tightens a pre-existing loose file", () => {
    const target = join(workDir, "loose.log");
    writeFileSync(target, "", { mode: 0o644 });
    closeSync(secureCreate(target));
    expect(mode(target)).toBe(0o600);
  });

  it("appends rather than truncating", () => {
    const target = join(workDir, "sink.log");
    writeFileSync(target, "first\n");
    const fd = secureCreate(target);
    closeSync(fd);
    expect(readFileSync(target, "utf-8")).toBe("first\n");
  });

  it("refuses to follow a symlink at the target path", () => {
    const real = join(workDir, "real.log");
    const link = join(workDir, "link.log");
    writeFileSync(real, "");
    symlinkSync(real, link);
    // O_NOFOLLOW makes the open itself fail rather than writing through the
    // link, which is the whole point: a planted symlink cannot redirect a
    // recording sink onto another file.
    expect(() => secureCreate(link)).toThrow(expect.objectContaining({ code: "ELOOP" }));
  });

  it("refuses a directory target", () => {
    // The kernel rejects a write-open of a directory before the regular-file
    // guard is reached; CPython surfaces the same IsADirectoryError.
    const target = join(workDir, "adir");
    mkdirSync(target);
    expect(() => secureCreate(target)).toThrow(expect.objectContaining({ code: "EISDIR" }));
  });

  it("honours a caller-supplied mode", () => {
    const target = join(workDir, "sink.log");
    closeSync(secureCreate(target, { mode: 0o640 }));
    expect(mode(target)).toBe(0o640);
  });
});

describe("secureOpenAppend", () => {
  it("returns a handle that appends", () => {
    const target = join(workDir, "sink.log");
    const first = secureOpenAppend(target);
    first.writeSync("one\n");
    first.close();
    const second = secureOpenAppend(target);
    second.writeSync("two\n");
    second.close();
    expect(readFileSync(target, "utf-8")).toBe("one\ntwo\n");
  });

  it("writes UTF-8", () => {
    const target = join(workDir, "sink.log");
    const handle = secureOpenAppend(target);
    handle.writeSync("你好\n");
    handle.close();
    expect(readFileSync(target, "utf-8")).toBe("你好\n");
  });

  it("creates the file with owner-only permissions", () => {
    const target = join(workDir, "sink.log");
    secureOpenAppend(target).close();
    expect(mode(target)).toBe(0o600);
  });
});

describe("loadAns", () => {
  it("decodes as latin-1 by default, so every byte survives", () => {
    const target = join(workDir, "art.ans");
    writeFileSync(target, Buffer.from([0x41, 0xb0, 0xff]));
    expect(loadAns(target)).toBe("A\xb0\xff");
  });

  it("honours an explicit encoding", () => {
    const target = join(workDir, "art.ans");
    writeFileSync(target, Buffer.from("你好", "utf-8"));
    expect(loadAns(target, "utf-8")).toBe("你好");
  });
});

describe("loadTxt", () => {
  it("decodes as UTF-8 by default", () => {
    const target = join(workDir, "notes.txt");
    writeFileSync(target, "你好", "utf-8");
    expect(loadTxt(target)).toBe("你好");
  });
});

describe("loadPalette", () => {
  it("returns a copy of the default palette when no path is given", () => {
    const palette = loadPalette(null);
    expect(palette).toStrictEqual(DEFAULT_PALETTE);
    palette[0] = 99;
    expect(DEFAULT_PALETTE[0]).toBe(0);
  });

  it("loads a sixteen-entry palette", () => {
    const target = join(workDir, "palette.json");
    const values = Array.from({ length: 16 }, (_, i) => i * 16);
    writeFileSync(target, JSON.stringify(values));
    expect(loadPalette(target)).toStrictEqual(values);
  });

  it("rejects a palette that is not a list of sixteen", () => {
    const target = join(workDir, "palette.json");
    writeFileSync(target, JSON.stringify([1, 2, 3]));
    expect(() => loadPalette(target)).toThrow("palette map must be a JSON list of 16 integers");
    writeFileSync(target, JSON.stringify({ a: 1 }));
    expect(() => loadPalette(target)).toThrow("palette map must be a JSON list of 16 integers");
  });

  it("rejects a non-integer or out-of-range entry", () => {
    const target = join(workDir, "palette.json");
    const base = Array.from({ length: 16 }, () => 0);
    for (const bad of [1.5, -1, 256, "1", null, true]) {
      writeFileSync(target, JSON.stringify([bad, ...base.slice(1)]));
      expect(() => loadPalette(target)).toThrow("palette map values must be integers in 0..255");
    }
  });

  it("accepts the range boundaries", () => {
    const target = join(workDir, "palette.json");
    const values = [0, 255, ...Array.from({ length: 14 }, () => 128)];
    writeFileSync(target, JSON.stringify(values));
    expect(loadPalette(target)).toStrictEqual(values);
  });
});
