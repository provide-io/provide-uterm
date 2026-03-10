import { routeApp } from "./router.js";
import type { AppBootstrap } from "./types.js";

function readBootstrap(): AppBootstrap {
  const script = document.getElementById("app-bootstrap");
  if (!(script instanceof HTMLScriptElement)) {
    throw new Error("Missing #app-bootstrap payload");
  }
  const parsed = JSON.parse(script.textContent || "{}") as Partial<AppBootstrap>;
  if (
    parsed.page_kind !== "dashboard" &&
    parsed.page_kind !== "session" &&
    parsed.page_kind !== "operator" &&
    parsed.page_kind !== "replay"
  ) {
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

export async function bootApp(): Promise<void> {
  const root = document.getElementById("app-root");
  if (!(root instanceof HTMLElement)) {
    throw new Error("Missing #app-root");
  }
  const bootstrap = readBootstrap();
  await routeApp(root, bootstrap);
}
