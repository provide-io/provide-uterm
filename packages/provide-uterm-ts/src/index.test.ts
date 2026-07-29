//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import * as uterm from "./index.ts";

const SOURCE_ROOT = new URL(".", import.meta.url).pathname;

/** Subpackages that have a barrel of their own. */
function subpackages(): string[] {
  return readdirSync(SOURCE_ROOT, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .filter((name) => {
      try {
        readFileSync(join(SOURCE_ROOT, name, "index.ts"));
        return true;
      } catch {
        return false;
      }
    });
}

/** The name a directory is exported under. */
function exportName(directory: string): string {
  return directory.replace(/-(\w)/g, (_match, character: string) => character.toUpperCase());
}

describe("the package root", () => {
  it("exports every subpackage", () => {
    // The package's own `exports` map points "." here. Before this existed,
    // importing the package by name failed outright while every subpath
    // import worked — the kind of break only a real consumer hits.
    const expected = subpackages()
      .filter((name) => name !== "testing" && name !== "react" && name !== "conformance")
      .map(exportName)
      .sort();
    expect(Object.keys(uterm).sort()).toStrictEqual(expected);
  });

  it("keeps the React bindings off the default entry", () => {
    // They are the only part of the package that needs React and a DOM.
    // Re-exporting them here would make every consumer — a CLI, a worker, a
    // test — load a user-interface library to reach the terminal emulator.
    // The `./*` subpath already reaches them, so a consumer that wants them
    // asks for them by name.
    expect(subpackages()).toContain("react");
    expect(Object.keys(uterm)).not.toContain("react");
  });

  it("keeps the conformance driver off the default entry", () => {
    // It reads scenario files and writes to a process's standard output, and
    // is run as an executable by the live harness rather than imported by a
    // consumer. Re-exporting it here would put `node:fs` on the path of every
    // browser and Worker build. `provide-uterm-ts/conformance` reaches it.
    expect(subpackages()).toContain("conformance");
    expect(Object.keys(uterm)).not.toContain("conformance");
  });

  it("does not export the testing helpers", () => {
    // They exist for this package's own tests and would be a promise to
    // consumers if they were part of the surface. They have no barrel at
    // all, which is what keeps them out.
    expect(readdirSync(SOURCE_ROOT, { withFileTypes: true }).map((entry) => entry.name)).toContain("testing");
    expect(subpackages()).not.toContain("testing");
    expect(Object.keys(uterm)).not.toContain("testing");
  });

  it("keeps each subpackage in its own namespace", () => {
    // Several name the same concept — a Screen, a Cursor, an InputMode — so
    // flattening would either collide or silently pick one.
    expect(uterm.hub).not.toBe(uterm.frames);
    expect(typeof uterm.pycompat.ipAddress).toBe("function");
    expect(typeof uterm.auth.fingerprintFromOpensshBlob).toBe("function");
  });

  it("carries the whole surface of a subpackage through", () => {
    expect(Object.keys(uterm.transports)).toContain("TelnetBuffer");
    expect(Object.keys(uterm.gateway)).toContain("ConnectionLimiter");
  });
});
