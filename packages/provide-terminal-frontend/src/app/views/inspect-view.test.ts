//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import type { AppBootstrap } from "../types.js";
import { renderInspect } from "./inspect-view.js";

function makeBootstrap(overrides: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind: "inspect",
    title: "Inspect",
    app_path: "/app",
    assets_path: "/assets",
    session_id: "tunnel-abc",
    ...overrides,
  };
}

describe("renderInspect", () => {
  it("renders the inspect shell with request list", async () => {
    const root = document.createElement("div");
    await renderInspect(root, makeBootstrap());
    expect(root.querySelector("#inspect-list")).toBeTruthy();
    expect(root.querySelector("#inspect-detail")).toBeTruthy();
  });

  it("renders toolbar with filter controls", async () => {
    const root = document.createElement("div");
    await renderInspect(root, makeBootstrap());
    expect(root.querySelector("#inspect-method-filter")).toBeTruthy();
    expect(root.querySelector("#inspect-url-filter")).toBeTruthy();
    expect(root.querySelector("#inspect-count")).toBeTruthy();
  });

  it("throws without session_id", async () => {
    const root = document.createElement("div");
    await expect(renderInspect(root, makeBootstrap({ session_id: undefined }))).rejects.toThrow(
      "inspect bootstrap missing session_id",
    );
  });
});
