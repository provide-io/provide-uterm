// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

import { rm } from "node:fs/promises";
import { resolve } from "node:path";

const frontendDir = resolve("packages/provide-uterm-server/src/provide/uterm/server/frontend");

await Promise.all([
  rm(resolve(frontendDir, "assets"), { recursive: true, force: true }),
  rm(resolve(frontendDir, ".vite"), { recursive: true, force: true }),
]);
