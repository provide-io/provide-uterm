//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  ConfigLoadError,
  deepMerge,
  loadServerDocument,
  normalizeDocument,
  parseTomlDocument,
  TABLE_SECTIONS,
} from "./index.ts";

interface LoaderGolden {
  table_sections: string[];
  merges: Array<{
    name: string;
    base: Record<string, unknown>;
    override: Record<string, unknown>;
    result: Record<string, unknown>;
  }>;
  mappings: Array<{
    name: string;
    document: Record<string, unknown>;
    kind: "accepted" | "structural" | "schema";
    error: string | null;
    message: string | null;
    sessions: unknown[] | null;
  }>;
  merge_is_pure: { base: Record<string, unknown>; override: Record<string, unknown> };
}

const golden = loadGolden<LoaderGolden>("configloader_golden.json");

const scratch = mkdtempSync(join(tmpdir(), "uterm-config-"));
afterAll(() => rmSync(scratch, { recursive: true, force: true }));

/** Rebuild the date values JSON flattened into strings. */
function rebuildDates(document: Record<string, unknown>): Record<string, unknown> {
  const rebuilt: Record<string, unknown> = { ...document };
  for (const [key, value] of Object.entries(rebuilt)) {
    if (typeof value === "string" && /^\d{4}-\d\d-\d\d|^\d\d:\d\d:\d\d/.test(value)) {
      rebuilt[key] = new Date(value.includes(":") && !value.includes("-") ? `1970-01-01T${value}` : value);
    }
  }
  return rebuilt;
}

/** Write a config file into the scratch directory. */
function writeConfig(name: string, body: string): string {
  const path = join(scratch, name);
  writeFileSync(path, body, "utf8");
  return path;
}

describe("merging a document over the defaults", () => {
  it.each(golden.merges)("$name", (record) => {
    expect(deepMerge(record.base, record.override)).toEqual(record.result);
  });

  it("keeps what the override does not mention", () => {
    // A partial `[auth]` section must leave the rest of the defaults
    // standing, or writing one line of config would erase the others.
    expect(deepMerge({ auth: { mode: "jwt", issuer: "x" } }, { auth: { mode: "header" } })).toEqual({
      auth: { mode: "header", issuer: "x" },
    });
  });

  it("merges tables at every depth", () => {
    expect(deepMerge({ a: { b: { c: 1, d: 2 } } }, { a: { b: { d: 3 } } })).toEqual({ a: { b: { c: 1, d: 3 } } });
  });

  it("replaces a list rather than merging it", () => {
    // Half of one list and half of another is not a configuration anybody
    // wrote.
    expect(deepMerge({ a: [1, 2, 3] }, { a: [4] })).toEqual({ a: [4] });
  });

  it("does not merge a table into a string", () => {
    // Strings are indexable here, so a merge that checked only the override
    // would produce a table of the base's characters.
    expect(deepMerge({ a: "abc" }, { a: { b: 2 } })).toEqual({ a: { b: 2 } });
  });

  it("lets either side win where only one is a table", () => {
    expect(deepMerge({ a: 1 }, { a: { b: 2 } })).toEqual({ a: { b: 2 } });
    expect(deepMerge({ a: { b: 2 } }, { a: 1 })).toEqual({ a: 1 });
  });

  it("does not treat a null as a table", () => {
    expect(deepMerge({ a: { b: 1 } }, { a: null })).toEqual({ a: null });
    expect(deepMerge({ a: null }, { a: { b: 1 } })).toEqual({ a: { b: 1 } });
  });

  it("leaves both sides as it found them", () => {
    // The defaults are shared; a merge that mutated them would leak one
    // load's configuration into the next.
    const base = { t: { a: 1 } };
    const override = { t: { b: 2 } };
    deepMerge(base, override);
    expect(base).toEqual(golden.merge_is_pure.base);
    expect(override).toEqual(golden.merge_is_pure.override);
  });
});

describe("the structural pass", () => {
  it("names every section that has to be a table", () => {
    expect([...TABLE_SECTIONS].sort()).toEqual(golden.table_sections);
  });

  it.each(golden.mappings.filter((entry) => entry.kind === "structural"))("$name", (record) => {
    // The structural pass runs before any schema, so its refusals are exactly
    // reproducible here. A recorded date arrives as a string through JSON and
    // is rebuilt, since what matters is that a date is not a table.
    const document = rebuildDates(record.document);
    // The reference names a bare date and a bare time separately; this engine
    // has one type for all three, so those two are checked for the section
    // rather than the type name.
    const expected = (record.message as string).replace(/\(got (date|time)\)/, "(got datetime)");
    expect(() => normalizeDocument(document)).toThrow(expected);
  });

  it.each(golden.mappings.filter((entry) => entry.kind !== "structural"))("$name is not refused here", (record) => {
    // Everything else is the schema's business, and the schema is not ported
    // yet — so this asserts only that the structural pass lets it through.
    expect(() => normalizeDocument(record.document)).not.toThrow();
  });

  it("says which section is wrong, and what it found", () => {
    // A schema error names a field nobody wrote; naming the section says
    // which line to look at.
    expect(() => normalizeDocument({ auth: "nope" })).toThrow("[auth] must be a table (got str)");
    expect(() => normalizeDocument({ ui: 7 })).toThrow("[ui] must be a table (got int)");
    expect(() => normalizeDocument({ pam: [1] })).toThrow("[pam] must be a table (got list)");
    expect(() => normalizeDocument({ profiles: true })).toThrow("[profiles] must be a table (got bool)");
    expect(() => normalizeDocument({ auth: null })).toThrow("[auth] must be a table (got NoneType)");
  });

  it("does not read a datetime as a table", () => {
    // TOML has a datetime type and the parser hands it back as a native date
    // object. In a language whose dates are objects that would merge field by
    // field into a section nobody wrote.
    expect(() => normalizeDocument({ auth: new Date("1979-05-27T07:32:00Z") })).toThrow(
      "[auth] must be a table (got datetime)",
    );
    const document = parseTomlDocument("auth = 1979-05-27T07:32:00Z\n");
    expect(() => normalizeDocument(document)).toThrow("[auth] must be a table");
  });

  it("names a type TOML cannot hold but the caller could pass", () => {
    // Exported, so it can be handed anything; nothing here comes from a TOML
    // file.
    expect(() => normalizeDocument({ auth: () => undefined })).toThrow("[auth] must be a table (got dict)");
  });

  it("uses the reference's type names, not the engine's", () => {
    // The message is read against a TOML file, so it has to name the types
    // that file could hold.
    expect(() => normalizeDocument({ auth: 1.5 })).toThrow("(got float)");
    expect(() => normalizeDocument({ auth: "x" })).toThrow("(got str)");
  });

  it("drops a session entry that is not a table", () => {
    // The one place the reference is lenient, and deliberately: one bad entry
    // should not stop a server that has other sessions to serve.
    const normalized = normalizeDocument({ sessions: [{ name: "a" }, "nope", 7, null] });
    expect(normalized.sessions).toEqual([{ name: "a" }]);
  });

  it("leaves a sessions value that is not a list alone", () => {
    // Filtering assumes a list; anything else is the schema's to refuse.
    expect(normalizeDocument({ sessions: "nope" }).sessions).toBe("nope");
    expect(normalizeDocument({ sessions: { name: "a" } }).sessions).toEqual({ name: "a" });
  });

  it("does not mutate the document it was given", () => {
    const document = { sessions: [{ name: "a" }, "nope"], auth: { mode: "jwt" } };
    normalizeDocument(document);
    expect(document.sessions).toHaveLength(2);
  });
});

describe("reading a config file", () => {
  it("parses TOML", () => {
    const document = parseTomlDocument('[auth]\nmode = "jwt"\n\n[[sessions]]\nname = "a"\n');
    expect(document).toEqual({ auth: { mode: "jwt" }, sessions: [{ name: "a" }] });
  });

  it("refuses TOML it cannot parse", () => {
    // Named as a config problem rather than surfacing the parser's own error,
    // which says nothing about which file was being read.
    expect(() => parseTomlDocument("[auth\nmode = ")).toThrow(ConfigLoadError);
  });

  it("loads a file from disk", () => {
    const path = writeConfig("basic.toml", '[auth]\nmode = "jwt"\n');
    expect(loadServerDocument(path)).toMatchObject({ auth: { mode: "jwt" } });
  });

  it("resolves a relative recording directory against the file", () => {
    // A config is read from wherever it lives and a server is started from
    // wherever the operator happens to be; resolving against the process
    // would put recordings somewhere neither of them chose.
    const path = writeConfig("relative.toml", '[recording]\ndirectory = "recordings"\n');
    const document = loadServerDocument(path);
    expect((document.recording as { directory: string }).directory).toBe(resolve(scratch, "recordings"));
  });

  it("keeps the rest of the recording section", () => {
    // Resolving the directory must not drop the settings beside it.
    const path = writeConfig(
      "full.toml",
      '[recording]\ndirectory = "recordings"\nenabled = true\nformat = "asciinema"\n',
    );
    expect(loadServerDocument(path).recording).toEqual({
      directory: resolve(scratch, "recordings"),
      enabled: true,
      format: "asciinema",
    });
  });

  it("leaves an absolute recording directory alone", () => {
    const path = writeConfig("absolute.toml", '[recording]\ndirectory = "/var/lib/uterm"\n');
    expect((loadServerDocument(path).recording as { directory: string }).directory).toBe("/var/lib/uterm");
  });

  it("leaves a document with no recording directory alone", () => {
    const path = writeConfig("norec.toml", '[auth]\nmode = "jwt"\n');
    expect(loadServerDocument(path).recording).toBeUndefined();
  });

  it("applies the structural pass to what it read", () => {
    const path = writeConfig("bad.toml", 'auth = "nope"\n');
    expect(() => loadServerDocument(path)).toThrow("[auth] must be a table");
  });

  it("reports a file it cannot read", () => {
    expect(() => loadServerDocument(join(scratch, "missing.toml"))).toThrow(ConfigLoadError);
  });
});
