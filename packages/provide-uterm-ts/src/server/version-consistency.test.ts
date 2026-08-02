//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseToml } from "smol-toml";
import { describe, expect, it } from "vitest";
import { SERVER_VERSION } from "./bootstrap.ts";

const repoRoot = fileURLToPath(new URL("../../../../", import.meta.url));

function readJson(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
}

describe("release version consistency", () => {
  it("uses one version across Python, npm workspaces, the lock, and the server", () => {
    const rootProject = parseToml(readFileSync(join(repoRoot, "pyproject.toml"), "utf8")).project as Record<
      string,
      unknown
    >;
    const expected = String(rootProject.version);
    expect(readFileSync(join(repoRoot, "VERSION"), "utf8").trim()).toBe(expected);

    for (const entry of readdirSync(join(repoRoot, "packages"), { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const versionPath = join(repoRoot, "packages", entry.name, "VERSION");
      try {
        expect(readFileSync(versionPath, "utf8").trim(), versionPath).toBe(expected);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
    }

    const rootManifest = readJson(join(repoRoot, "package.json"));
    const lock = readJson(join(repoRoot, "package-lock.json"));
    const lockPackages = lock.packages as Record<string, Record<string, unknown>>;
    for (const workspace of rootManifest.workspaces as string[]) {
      const manifest = readJson(join(repoRoot, workspace, "package.json"));
      if (typeof manifest.version !== "string") continue;
      expect(manifest.version, workspace).toBe(expected);
      expect(lockPackages[workspace]?.version, `${workspace} lock entry`).toBe(expected);
    }

    expect(SERVER_VERSION).toBe(expected);
    expect(readFileSync(join(repoRoot, "CHANGELOG.md"), "utf8")).toContain(`## [${expected}] — 2026-08-01`);
  });
});
