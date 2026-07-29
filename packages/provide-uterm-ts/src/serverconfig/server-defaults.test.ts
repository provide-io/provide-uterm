//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  deepMerge,
  SERVER_CONFIG_DEFAULTS,
  SERVER_CONFIG_SECTIONS,
  SERVER_CONFIG_TOP_LEVEL,
  TABLE_SECTIONS,
} from "./index.ts";

interface DefaultsGolden {
  config: Record<string, unknown>;
  sections: string[];
  top_level_scalars: string[];
}

const golden = loadGolden<DefaultsGolden>("serverdefaults_golden.json");

describe("the server's default configuration", () => {
  it("matches the reference value for value", () => {
    // Transcribed from this corpus once, so it could not have failed the day
    // it was written. Its job is drift: it fails the moment a default changes
    // on either side.
    expect(SERVER_CONFIG_DEFAULTS).toEqual(golden.config);
  });

  it("has the sections the schema has", () => {
    expect([...SERVER_CONFIG_SECTIONS]).toEqual(golden.sections);
    expect([...SERVER_CONFIG_TOP_LEVEL]).toEqual(golden.top_level_scalars);
  });

  it("binds to loopback and not to every address", () => {
    // The default posture, not a description of one: a server started with no
    // config file is not reachable off-box.
    expect((SERVER_CONFIG_DEFAULTS.server as Record<string, unknown>).host).toBe("127.0.0.1");
  });

  it("is frozen all the way down", () => {
    // Shared by every load, so a caller mutating it would change what the
    // *next* load defaults to.
    expect(Object.isFrozen(SERVER_CONFIG_DEFAULTS)).toBe(true);
    expect(Object.isFrozen(SERVER_CONFIG_DEFAULTS.auth)).toBe(true);
    expect(() => {
      (SERVER_CONFIG_DEFAULTS as Record<string, unknown>).environment = "changed";
    }).toThrow();
  });

  it("survives a merge without being changed by it", () => {
    // Which is what the loader does to it on every load.
    const before = JSON.stringify(SERVER_CONFIG_DEFAULTS);
    const merged = deepMerge(SERVER_CONFIG_DEFAULTS, { auth: { mode: "header" } });
    expect((merged.auth as Record<string, unknown>).mode).toBe("header");
    expect(JSON.stringify(SERVER_CONFIG_DEFAULTS)).toBe(before);
  });

  it("keeps every default a document does not mention", () => {
    // The point of merging rather than replacing: writing one line of config
    // must not erase the rest.
    const merged = deepMerge(SERVER_CONFIG_DEFAULTS, { auth: { mode: "header" } });
    const auth = merged.auth as Record<string, unknown>;
    const defaults = SERVER_CONFIG_DEFAULTS.auth as Record<string, unknown>;
    expect(Object.keys(auth).sort()).toEqual(Object.keys(defaults).sort());
    expect(auth.clock_skew_seconds).toBe(defaults.clock_skew_seconds);
  });
});

describe("what the structural pass covers", () => {
  it("checks fewer sections than the schema has", () => {
    // A gap in the reference, pinned rather than closed: `audit` and
    // `governance` are tables in the schema but are not in the loader's list,
    // so a document writing one as a string gets the schema's complaint about
    // a field nobody wrote instead of the friendly one naming the section.
    const unchecked = golden.sections.filter((section) => !TABLE_SECTIONS.has(section));
    expect(unchecked).toEqual(["audit", "governance"]);
  });

  it("checks nothing the schema does not have", () => {
    // The other direction would be worse: a section checked but absent from
    // the schema would refuse a document the reference accepts.
    for (const section of TABLE_SECTIONS) {
      expect(golden.sections).toContain(section);
    }
  });
});
