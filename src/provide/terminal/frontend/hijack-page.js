import { apiJson, requireElement } from "./server-common.js";
class HijackDemoPage {
    constructor() {
        const params = new URLSearchParams(window.location.search);
        this.workerId = params.get("worker") || "demo-session";
        const appElement = requireElement("#app");
        this.modeElement = requireElement("#demo-mode");
        this.statusElement = requireElement("#demo-session-status");
        this.noteElement = requireElement("#demo-session-note");
        const HijackWidget = window.ProvideHijack;
        if (typeof HijackWidget !== "function") {
            throw new Error("ProvideHijack is not available");
        }
        this.widget = new HijackWidget(appElement, { workerId: this.workerId });
        requireElement("#demo-apply").addEventListener("click", () => {
            void this.applyMode();
        });
        requireElement("#demo-reset").addEventListener("click", () => {
            void this.resetSession();
        });
    }
    async loadSession() {
        try {
            const data = await apiJson(`/api/sessions/${encodeURIComponent(this.workerId)}`);
            this.modeElement.value = data.input_mode || "hijack";
            const state = data.lifecycle_state === "paused" ? "paused" : "live";
            this.statusElement.textContent = `${data.display_name || "Session"} | ${data.input_mode || "hijack"} | ${state}`;
            this.statusElement.classList.remove("error");
            this.noteElement.textContent = "The demo worker accepts input while hijacked.";
        }
        catch (error) {
            this.statusElement.textContent = `Session load failed: ${String(error)}`;
            this.statusElement.classList.add("error");
        }
    }
    async applyMode() {
        try {
            await apiJson(`/api/sessions/${encodeURIComponent(this.workerId)}/mode`, "POST", {
                input_mode: this.modeElement.value,
            });
            await this.loadSession();
        }
        catch (error) {
            this.statusElement.textContent = `Mode switch failed: ${String(error)}`;
            this.statusElement.classList.add("error");
        }
    }
    async resetSession() {
        try {
            await apiJson(`/api/sessions/${encodeURIComponent(this.workerId)}/restart`, "POST");
            await this.loadSession();
            this.noteElement.textContent = "Session reset.";
        }
        catch (error) {
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
