//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AppBootstrap } from "./api/types";

const VALID_PAGE_KINDS = new Set(["dashboard", "session", "operator", "replay", "connect"]);

export function readBootstrap(): AppBootstrap {
  const script = document.getElementById("app-bootstrap");
  if (!(script instanceof HTMLScriptElement)) {
    throw new Error("Missing #app-bootstrap payload");
  }
  const parsed = JSON.parse(script.textContent || "{}") as Partial<AppBootstrap>;
  if (!parsed.page_kind || !VALID_PAGE_KINDS.has(parsed.page_kind)) {
    throw new Error("Invalid page bootstrap");
  }
  if (
    typeof parsed.title !== "string" ||
    typeof parsed.app_path !== "string" ||
    typeof parsed.assets_path !== "string"
  ) {
    throw new Error("Incomplete page bootstrap");
  }
  return parsed as AppBootstrap;
}
