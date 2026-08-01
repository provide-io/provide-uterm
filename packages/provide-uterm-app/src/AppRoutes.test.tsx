//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AppBootstrap, AppPageKind } from "./api/types";

vi.mock("./components/dashboard/DashboardPage", () => ({ DashboardPage: () => <div>dashboard page</div> }));
vi.mock("./components/connect/ConnectPage", () => ({ ConnectPage: () => <div>connect page</div> }));
vi.mock("./components/operator/OperatorPage", () => ({ OperatorPage: () => <div>operator page</div> }));
vi.mock("./components/session/SessionPage", () => ({ SessionPage: () => <div>session page</div> }));
vi.mock("./components/replay/ReplayPage", () => ({ ReplayPage: () => <div>replay page</div> }));
vi.mock("./components/inspect/InspectPage", () => ({ InspectPage: () => <div>inspect page</div> }));

import { App } from "./App";

function bootstrap(page_kind: AppPageKind): AppBootstrap {
  return { page_kind, title: "App", app_path: "/app", assets_path: "/assets", session_id: "s1" };
}

describe("App operational routing", () => {
  for (const kind of ["dashboard", "connect", "operator", "session", "replay", "inspect"] as const) {
    it(`routes ${kind} bootstraps to the ${kind} view`, () => {
      render(<App bootstrap={bootstrap(kind)} />);
      expect(screen.getByText(`${kind} page`)).toBeInTheDocument();
    });
  }
});
