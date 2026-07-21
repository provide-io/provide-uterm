// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Copy Vite's leading-dot manifest to a package-data-safe path.
// setuptools recursive package-data globs omit directories named `.vite/`,
// so wheels would otherwise ship without the React app manifest.

import { copyFile, access } from "node:fs/promises";
import { resolve } from "node:path";

const frontendDir = resolve("packages/provide-uterm-server/src/provide/uterm/server/frontend");
const src = resolve(frontendDir, ".vite", "manifest.json");
const dest = resolve(frontendDir, "vite-manifest.json");

try {
  await access(src);
} catch {
  console.warn("publish-frontend-manifest: no .vite/manifest.json — skip");
  process.exit(0);
}

await copyFile(src, dest);
console.log("publish-frontend-manifest: wrote frontend/vite-manifest.json");
