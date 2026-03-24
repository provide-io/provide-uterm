//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./views/connect-view.js", () => ({
  renderConnect: vi.fn(),
}));
vi.mock("./views/dashboard-view.js", () => ({
  renderDashboard: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("./views/operator-view.js", () => ({
  renderOperator: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("./views/replay-view.js", () => ({
  renderReplay: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("./views/session-view.js", () => ({
  renderSession: vi.fn().mockResolvedValue(undefined),
}));

import { routeApp } from "./router.js";
import type { AppBootstrap } from "./types.js";
import * as connectView from "./views/connect-view.js";
import * as dashboardView from "./views/dashboard-view.js";
import * as operatorView from "./views/operator-view.js";
import * as replayView from "./views/replay-view.js";
import * as sessionView from "./views/session-view.js";

function makeBootstrap(page_kind: AppBootstrap["page_kind"], extra: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind,
    title: "Test",
    app_path: "/app",
    assets_path: "/assets",
    ...extra,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("routeApp", () => {
  it("calls renderConnect for connect page_kind", async () => {
    const root = document.createElement("div");
    const bootstrap = makeBootstrap("connect");
    await routeApp(root, bootstrap);
    expect(connectView.renderConnect).toHaveBeenCalledWith(root, bootstrap);
    expect(dashboardView.renderDashboard).not.toHaveBeenCalled();
  });

  it("calls renderDashboard for dashboard page_kind", async () => {
    const root = document.createElement("div");
    const bootstrap = makeBootstrap("dashboard");
    await routeApp(root, bootstrap);
    expect(dashboardView.renderDashboard).toHaveBeenCalledWith(root, bootstrap);
  });

  it("calls renderSession for session page_kind", async () => {
    const root = document.createElement("div");
    const bootstrap = makeBootstrap("session", { session_id: "s1" });
    await routeApp(root, bootstrap);
    expect(sessionView.renderSession).toHaveBeenCalledWith(root, bootstrap);
  });

  it("calls renderOperator for operator page_kind", async () => {
    const root = document.createElement("div");
    const bootstrap = makeBootstrap("operator", { session_id: "s1" });
    await routeApp(root, bootstrap);
    expect(operatorView.renderOperator).toHaveBeenCalledWith(root, bootstrap);
  });

  it("calls renderReplay for replay page_kind", async () => {
    const root = document.createElement("div");
    const bootstrap = makeBootstrap("replay", { session_id: "s1" });
    await routeApp(root, bootstrap);
    expect(replayView.renderReplay).toHaveBeenCalledWith(root, bootstrap);
  });
});
