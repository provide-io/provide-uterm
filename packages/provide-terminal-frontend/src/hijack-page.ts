//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { apiJson, type ProvideHijackConstructor, requireElement, type SessionStatus } from "./server-common.js";

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
      const data = await apiJson<SessionStatus>(`/api/sessions/${encodeURIComponent(this.workerId)}`);
      this.modeElement.value = data.input_mode || "hijack";
      const state = data.lifecycle_state === "paused" ? "paused" : "live";
      this.statusElement.textContent = `${data.display_name || "Session"} | ${data.input_mode || "hijack"} | ${state}`;
      this.statusElement.classList.remove("error");
      this.noteElement.textContent = "The demo worker accepts input while hijacked.";
    } catch (error) {
      this.statusElement.textContent = `Session load failed: ${String(error)}`;
      this.statusElement.classList.add("error");
    }
  }

  async applyMode(): Promise<void> {
    try {
      await apiJson<SessionStatus>(`/api/sessions/${encodeURIComponent(this.workerId)}/mode`, "POST", {
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
      await apiJson<SessionStatus>(`/api/sessions/${encodeURIComponent(this.workerId)}/restart`, "POST");
      await this.loadSession();
      this.noteElement.textContent = "Session reset.";
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
