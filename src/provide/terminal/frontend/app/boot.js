import { routeApp } from "./router.js";
function readBootstrap() {
    const script = document.getElementById("app-bootstrap");
    if (!(script instanceof HTMLScriptElement)) {
        throw new Error("Missing #app-bootstrap payload");
    }
    const parsed = JSON.parse(script.textContent || "{}");
    if (parsed.page_kind !== "dashboard" &&
        parsed.page_kind !== "session" &&
        parsed.page_kind !== "operator" &&
        parsed.page_kind !== "replay") {
        throw new Error("Invalid page bootstrap");
    }
    if (typeof parsed.title !== "string" ||
        typeof parsed.app_path !== "string" ||
        typeof parsed.assets_path !== "string") {
        throw new Error("Incomplete page bootstrap");
    }
    return parsed;
}
export async function bootApp() {
    const root = document.getElementById("app-root");
    if (!(root instanceof HTMLElement)) {
        throw new Error("Missing #app-root");
    }
    const bootstrap = readBootstrap();
    await routeApp(root, bootstrap);
}
