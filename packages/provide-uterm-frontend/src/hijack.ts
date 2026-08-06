import {
  type UtermSessionElement,
  registerUtermSessionElement,
} from "./session-element.js";

export * from "./session-element.js";

registerUtermSessionElement();

import { startReconnectAnim, stopReconnectAnim } from "./hijack-websocket.js";

declare global {
  interface Window {
    __testHooks_startReconnectAnim?: typeof startReconnectAnim;
    __testHooks_stopReconnectAnim?: typeof stopReconnectAnim;
  }
}

if (typeof window !== "undefined") {
  window.__testHooks_startReconnectAnim = startReconnectAnim;
  window.__testHooks_stopReconnectAnim = stopReconnectAnim;

  // Self-assemble if running in vanilla mode
  const sessionEl = document.querySelector("uterm-session#app-root") as UtermSessionElement | null;
  const bootstrapScript = document.getElementById("app-bootstrap");
  if (sessionEl && bootstrapScript) {
    try {
      const bootstrap = JSON.parse(bootstrapScript.textContent || "{}");
      sessionEl.config = {
        workerId: bootstrap.session_id,
        showAnalysis: bootstrap.page_kind === "operator",
        mobileKeys: bootstrap.page_kind === "operator",
        role: bootstrap.share_role,
      };
      sessionEl.connect();
    } catch (err) {
      console.error("ProvideHijack: failed to read bootstrap", err);
    }
  }
}
