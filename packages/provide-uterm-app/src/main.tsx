//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { readBootstrap } from "./bootstrap";
import "./styles/tokens.css";
import "provide-uterm-frontend/deckmux.css";

const rootEl = document.getElementById("app-root");
if (!rootEl) throw new Error("Missing #app-root");

const bootstrap = readBootstrap();

createRoot(rootEl).render(
  <StrictMode>
    <App bootstrap={bootstrap} />
  </StrictMode>,
);
