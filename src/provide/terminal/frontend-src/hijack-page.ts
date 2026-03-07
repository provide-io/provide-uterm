import { type ProvideHijackConstructor, apiJson, requireElement } from "./server-common.js";

declare global {
  interface Window {
    ProvideHijack?: ProvideHijackConstructor;
    demoHijack?: {
      widget: unknown;
      loadSession: () => Promise<void>;
      applyMode: () => Promise<void>;
      resetSession: () => Promise<void>;
      workerId: string;
    };
  }
}

interface DemoSessionPayload {
  title?: string;
  input_mode?: string;
  paused?: boolean;
  pending_banner?: string | null;
}

class HijackDemoPage {
  readonly workerId: string;
  private readonly modeElement: HTMLSelectElement;
  private readonly statusElement: HTMLElement;
  private readonly noteElement: HTMLElement;
  readonly widget: unknown;

  constructor() {
    const params = new URLSearchParams(window.location.search);
    this.workerId = params.get("worker") || "demo-session";
    const appElement = requireElement<HTMLElement>("#app");
    this.modeElement = requireElement<HTMLSelectElement>("#demo-mode");
    this.statusElement = requireElement<HTMLElement>("#demo-session-status");
    this.noteElement = requireElement<HTMLElement>("#demo-session-note");
    const HijackWidget = window.ProvideHijack;
    if (typeof HijackWidget !== "function") {
      throw new Error("ProvideHijack is not available");
    }
    this.widget = new HijackWidget(appElement, { workerId: this.workerId });
    requireElement<HTMLButtonElement>("#demo-apply").addEventListener("click", () => {
      void this.applyMode();
    });
    requireElement<HTMLButtonElement>("#demo-reset").addEventListener("click", () => {
      void this.resetSession();
    });
  }

  async loadSession(): Promise<void> {
    try {
      const data = await apiJson<DemoSessionPayload>(`/demo/session/${encodeURIComponent(this.workerId)}`);
      this.modeElement.value = data.input_mode || "hijack";
      this.statusElement.textContent = `${data.title || "Demo Session"} | ${data.input_mode || "hijack"} | ${
        data.paused ? "paused" : "live"
      }`;
      this.statusElement.classList.remove("error");
      this.noteElement.textContent = data.pending_banner || "The demo worker accepts input while hijacked.";
    } catch (error) {
      this.statusElement.textContent = `Session load failed: ${String(error)}`;
      this.statusElement.classList.add("error");
    }
  }

  async applyMode(): Promise<void> {
    try {
      await apiJson<DemoSessionPayload>(`/demo/session/${encodeURIComponent(this.workerId)}/mode`, "POST", {
        input_mode: this.modeElement.value,
      });
      await this.loadSession();
    } catch (error) {
      this.statusElement.textContent = `Mode switch failed: ${String(error)}`;
      this.statusElement.classList.add("error");
    }
  }

  async resetSession(): Promise<void> {
    try {
      await apiJson<DemoSessionPayload>(`/demo/session/${encodeURIComponent(this.workerId)}/reset`, "POST");
      await this.loadSession();
    } catch (error) {
      this.statusElement.textContent = `Reset failed: ${String(error)}`;
      this.statusElement.classList.add("error");
    }
  }
}

const page = new HijackDemoPage();
void page.loadSession();
window.demoHijack = {
  widget: page.widget,
  loadSession: () => page.loadSession(),
  applyMode: () => page.applyMode(),
  resetSession: () => page.resetSession(),
  workerId: page.workerId,
};
