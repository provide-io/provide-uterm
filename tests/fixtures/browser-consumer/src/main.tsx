//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import {
  App,
  type AppBootstrap,
  type HijackHostProps,
  type ReplayPageProps,
  type SessionPageProps,
  type TerminalHostProps,
  applyThemeTokens,
  createUtermExtensionRegistry,
} from "provide-uterm-app";
import "provide-uterm-app/styles/tokens.css";
import {
  TerminalElement,
  UtermSessionElement,
  registerUtermElements,
} from "provide-uterm-frontend";
import { DeckMux } from "provide-uterm-frontend/deckmux";
import "provide-uterm-frontend/deckmux.css";
import { createRoot } from "react-dom/client";

const bootstrap: AppBootstrap = {
  page_kind: "consumer-reports",
  title: "Consumer Reports",
  app_path: "/",
  assets_path: "/assets",
};
const registry = createUtermExtensionRegistry();
registry.register({
  id: "fixture",
  pages: [{ kind: "consumer-reports", component: ({ bootstrap: page }) => <h1>{page.title}</h1> }],
});
applyThemeTokens(document.documentElement, { "--bg-primary": "#010203" });
registerUtermElements();

const exportedContracts: [
  typeof DeckMux,
  typeof TerminalElement,
  typeof UtermSessionElement,
  SessionPageProps | null,
  ReplayPageProps | null,
  TerminalHostProps | null,
  HijackHostProps | null,
] = [DeckMux, TerminalElement, UtermSessionElement, null, null, null, null];
void exportedContracts;

createRoot(document.getElementById("root")!).render(
  <App bootstrap={bootstrap} extensions={registry} />,
);
