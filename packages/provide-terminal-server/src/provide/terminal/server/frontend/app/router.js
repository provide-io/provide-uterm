//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { renderConnect } from "./views/connect-view.js";
import { renderDashboard } from "./views/dashboard-view.js";
import { renderInspect } from "./views/inspect-view.js";
import { renderOperator } from "./views/operator-view.js";
import { renderReplay } from "./views/replay-view.js";
import { renderSession } from "./views/session-view.js";
export async function routeApp(root, bootstrap) {
    switch (bootstrap.page_kind) {
        case "connect":
            void renderConnect(root, bootstrap);
            return;
        case "dashboard":
            await renderDashboard(root, bootstrap);
            return;
        case "session":
            await renderSession(root, bootstrap);
            return;
        case "operator":
            await renderOperator(root, bootstrap);
            return;
        case "replay":
            await renderReplay(root, bootstrap);
            return;
        case "inspect":
            await renderInspect(root, bootstrap);
            return;
    }
}
