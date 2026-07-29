//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Make node-pty's spawn helper executable.
 *
 * node-pty ships a per-platform `spawn-helper` binary, and every PTY it opens
 * on a Unix runs through it. npm extracts the prebuild without the execute
 * bit, so a freshly installed tree fails every spawn with `posix_spawnp
 * failed` — a message that says nothing about the cause.
 *
 * Idempotent, and silent when there is nothing to do.
 */

import { chmodSync, existsSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

const EXECUTABLE_BITS = 0o111;

/** Where node-pty was installed, wherever the workspace hoisted it. */
function nodePtyRoot() {
  const require = createRequire(import.meta.url);
  try {
    return dirname(require.resolve("node-pty/package.json"));
  } catch {
    return undefined;
  }
}

const root = nodePtyRoot();
if (root === undefined) {
  // Nothing installed is not a failure: the PTY connector is optional.
  process.exit(0);
}

const helper = join(root, "prebuilds", `${process.platform}-${process.arch}`, "spawn-helper");
if (!existsSync(helper)) {
  // Windows has no helper, and a source build puts its own in place.
  process.exit(0);
}

const mode = statSync(helper).mode;
if ((mode & EXECUTABLE_BITS) === EXECUTABLE_BITS) {
  process.exit(0);
}

chmodSync(helper, mode | EXECUTABLE_BITS);
process.stdout.write(`made node-pty's spawn helper executable: ${helper}\n`);
