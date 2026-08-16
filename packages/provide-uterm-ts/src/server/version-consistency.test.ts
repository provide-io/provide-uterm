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

/**
 * Directory names under packages/ that belong to another repository.
 *
 * packages/provide-telemetry is a git submodule, released on its own cadence —
 * it sits at 0.7.0 while this repo is at 0.5.0, and its VERSION is not ours to
 * keep in step. scripts/repo_paths.py already carries this rule for the SPDX
 * walker and the docs-accuracy checker, both of which "got it wrong in the same
 * way" when the submodule landed; this check is the one that never got updated.
 *
 * Parsed from .gitmodules rather than shelled out to `git submodule`, matching
 * that helper: the answer must not depend on whether the submodule happens to
 * be initialised, which it is not in a fresh clone.
 */
function submoduleNames(): Set<string> {
  let config: string;
  try {
    config = readFileSync(join(repoRoot, ".gitmodules"), "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    return new Set();
  }
  const names = new Set<string>();
  for (const line of config.split("\n")) {
    const stripped = line.trim();
    if (!stripped.startsWith("path")) continue;
    const value = stripped.slice(stripped.indexOf("=") + 1).trim();
    const [parent, name] = value.split("/");
    if (parent === "packages" && name) names.add(name);
  }
  return names;
}

describe("release version consistency", () => {
  it("uses one version across Python, npm workspaces, the lock, and the server", () => {
    const rootProject = parseToml(readFileSync(join(repoRoot, "pyproject.toml"), "utf8")).project as Record<
      string,
      unknown
    >;
    const expected = String(rootProject.version);
    expect(readFileSync(join(repoRoot, "VERSION"), "utf8").trim()).toBe(expected);

    const foreign = submoduleNames();
    let checked = 0;
    for (const entry of readdirSync(join(repoRoot, "packages"), { withFileTypes: true })) {
      if (!entry.isDirectory() || foreign.has(entry.name)) continue;
      const versionPath = join(repoRoot, "packages", entry.name, "VERSION");
      try {
        expect(readFileSync(versionPath, "utf8").trim(), versionPath).toBe(expected);
        checked += 1;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
    }
    // Skipping is only safe while it stays narrow. A parse slip in .gitmodules
    // that swallowed every name would empty this loop, and an assertion that
    // checks nothing passes just as quietly as one that checks everything.
    expect(checked, "no packages/*/VERSION files were checked").toBeGreaterThan(0);

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

  it("keeps pre-release hardening inside the dated release section", () => {
    const changelog = readFileSync(join(repoRoot, "CHANGELOG.md"), "utf8");
    const unreleasedStart = changelog.indexOf("## [Unreleased]");
    const releaseStart = changelog.indexOf("## [0.5.0] — 2026-08-01");

    expect(unreleasedStart).toBeGreaterThanOrEqual(0);
    expect(releaseStart).toBeGreaterThan(unreleasedStart);
    expect(changelog.slice(unreleasedStart + "## [Unreleased]".length, releaseStart).trim()).toBe("");
    expect(changelog.indexOf("### Post-audit hardening", releaseStart)).toBeGreaterThan(releaseStart);
  });
});
