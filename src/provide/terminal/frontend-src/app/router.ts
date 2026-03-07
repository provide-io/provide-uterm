import { renderDashboard } from "./views/dashboard-view.js";
import { renderOperator } from "./views/operator-view.js";
import { renderReplay } from "./views/replay-view.js";
import { renderSession } from "./views/session-view.js";
import type { AppBootstrap } from "./types.js";

export async function routeApp(root: HTMLElement, bootstrap: AppBootstrap): Promise<void> {
  switch (bootstrap.page_kind) {
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
  }
}
