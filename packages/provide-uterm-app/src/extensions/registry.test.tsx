//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import type { AppBootstrap } from "../api/types";
import {
  applyThemeTokens,
  createUtermExtensionRegistry,
  type UtermAuthContext,
} from "./registry";

const bootstrap: AppBootstrap = {
  page_kind: "consumer-reports",
  title: "Consumer Reports",
  app_path: "/",
  assets_path: "/assets",
};

describe("Uterm consumer extension registry", () => {
  it("registers theme, navigation, page kind, and authentication adapters", async () => {
    const context: UtermAuthContext = {
      subject: "external-user-1",
      roles: ["reader"],
      attributes: { displayName: "Example User" },
    };
    const registry = createUtermExtensionRegistry();
    registry.register({
      id: "consumer",
      themeTokens: {
        "--bg-primary": "#050302",
        "--text-primary": "#ffd166",
      },
      navigation: [
        { id: "reports", label: "Reports", href: "/reports", pageKind: "consumer-reports" },
      ],
      pages: [
        {
          kind: "consumer-reports",
          component: ({ bootstrap: page }) => <h1>{page.title}</h1>,
        },
      ],
      auth: {
        resolve: async () => context,
        authorize: (identity, capability) =>
          identity.roles.includes("reader") && capability === "reports.read",
      },
    });

    const snapshot = registry.snapshot();
    expect(snapshot.navigation).toEqual([
      { id: "reports", label: "Reports", href: "/reports", pageKind: "consumer-reports" },
    ]);
    expect(await snapshot.auth?.resolve()).toEqual(context);
    expect(snapshot.auth?.authorize(context, "reports.read")).toBe(true);
    expect(snapshot.auth?.authorize(context, "operator.hijack")).toBe(false);

    applyThemeTokens(document.documentElement, snapshot.themeTokens);
    expect(document.documentElement.style.getPropertyValue("--bg-primary")).toBe("#050302");
    expect(document.documentElement.style.getPropertyValue("--text-primary")).toBe("#ffd166");

    render(<App bootstrap={bootstrap} extensions={registry} />);
    expect(screen.getByRole("heading", { name: "Consumer Reports" })).toBeInTheDocument();
  });

  it("rejects duplicate ids, page kinds, navigation ids, and malformed tokens", () => {
    const registry = createUtermExtensionRegistry();
    registry.register({
      id: "first",
      pages: [{ kind: "consumer-page", component: () => null }],
      navigation: [{ id: "consumer-nav", label: "Consumer", href: "/consumer" }],
    });

    expect(() => registry.register({ id: "first" })).toThrow(/extension id/i);
    expect(() =>
      registry.register({
        id: "duplicate-page",
        pages: [{ kind: "consumer-page", component: () => null }],
      }),
    ).toThrow(/page kind/i);
    expect(() =>
      registry.register({
        id: "duplicate-nav",
        navigation: [{ id: "consumer-nav", label: "Other", href: "/other" }],
      }),
    ).toThrow(/navigation id/i);
    expect(() => registry.register({ id: "bad-theme", themeTokens: { color: "red" } })).toThrow(
      /theme token/i,
    );
  });

  it("rejects duplicates inside one extension without partially registering it", () => {
    const registry = createUtermExtensionRegistry();

    expect(() =>
      registry.register({
        id: "broken",
        navigation: [
          { id: "same", label: "First", href: "/first" },
          { id: "same", label: "Second", href: "/second" },
        ],
      }),
    ).toThrow(/navigation id/i);

    registry.register({
      id: "broken",
      navigation: [{ id: "same", label: "Recovered", href: "/recovered" }],
    });
    expect(registry.snapshot().navigation).toEqual([
      { id: "same", label: "Recovered", href: "/recovered" },
    ]);

    expect(() =>
      registry.register({
        id: "duplicate-pages",
        pages: [
          { kind: "same-page", component: () => null },
          { kind: "same-page", component: () => null },
        ],
      }),
    ).toThrow(/page kind/i);
    expect(registry.resolvePage("same-page")).toBeNull();
  });

  it.each(["dashboard", "session", "operator", "replay", "connect", "inspect"])(
    "reserves the built-in %s page kind",
    (kind) => {
      const registry = createUtermExtensionRegistry();
      expect(() =>
        registry.register({ id: `override-${kind}`, pages: [{ kind, component: () => null }] }),
      ).toThrow(/reserved page kind/i);
    },
  );

  it("applies theme tokens atomically", () => {
    const target = document.createElement("div");
    expect(() =>
      applyThemeTokens(target, { "--valid-token": "#fff", invalid: "#000" }),
    ).toThrow(/theme token/i);
    expect(target.getAttribute("style")).toBeNull();
  });
});
