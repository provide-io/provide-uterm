//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AppBootstrap } from "../../api/types";
import { AppHeader } from "./AppHeader";

const BOOTSTRAP: AppBootstrap = {
  page_kind: "dashboard",
  title: "Test Terminal",
  app_path: "/app",
  assets_path: "/assets",
};

describe("AppHeader", () => {
  it("renders a header element", () => {
    render(<AppHeader bootstrap={BOOTSTRAP} />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
  });

  it("renders Dashboard crumb by default", () => {
    render(<AppHeader bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
  });

  it("Dashboard crumb links to app_path root", () => {
    render(<AppHeader bootstrap={BOOTSTRAP} crumbs={[{ label: "Replay" }]} />);
    const dashLink = screen.getByText("Dashboard");
    expect(dashLink.tagName).toBe("A");
    expect(dashLink).toHaveAttribute("href", "/app/");
  });

  it("renders additional crumbs", () => {
    render(<AppHeader bootstrap={BOOTSTRAP} crumbs={[{ label: "Replay" }]} />);
    expect(screen.getByText("Replay")).toBeInTheDocument();
  });

  it("renders crumb separators", () => {
    render(<AppHeader bootstrap={BOOTSTRAP} crumbs={[{ label: "Replay" }]} />);
    expect(screen.getByText("/")).toBeInTheDocument();
  });

  it("last crumb is not a link", () => {
    render(
      <AppHeader
        bootstrap={BOOTSTRAP}
        crumbs={[
          { label: "Session", href: "/app/session/1" },
          { label: "Replay" },
        ]}
      />,
    );
    const replay = screen.getByText("Replay");
    expect(replay.tagName).toBe("SPAN");
  });

  it("middle crumb with href is rendered as a link", () => {
    render(
      <AppHeader
        bootstrap={BOOTSTRAP}
        crumbs={[
          { label: "Session", href: "/app/session/1" },
          { label: "Replay" },
        ]}
      />,
    );
    const sessionLink = screen.getByText("Session");
    expect(sessionLink.tagName).toBe("A");
    expect(sessionLink).toHaveAttribute("href", "/app/session/1");
  });

  it("renders right content when provided", () => {
    render(
      <AppHeader bootstrap={BOOTSTRAP} right={<button type="button">Action</button>} />,
    );
    expect(screen.getByText("Action")).toBeInTheDocument();
  });

  it("does not render right section when not provided", () => {
    const { container } = render(<AppHeader bootstrap={BOOTSTRAP} />);
    const header = container.querySelector("header");
    // Only the nav element, no right div
    expect(header?.children).toHaveLength(1);
  });

  it("renders the sole crumb as a span (not a link) when only Dashboard exists", () => {
    render(<AppHeader bootstrap={BOOTSTRAP} />);
    const dashboard = screen.getByText("Dashboard");
    expect(dashboard.tagName).toBe("SPAN");
  });
});
