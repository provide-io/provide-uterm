import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type UtermSessionElement,
  registerUtermSessionElement,
} from "./session-element.js";
import { encodeControlFrame } from "./hijack-codec.js";

registerUtermSessionElement();

// Reuse mocks logic
class MockWebSocket {
  readyState = 1; // OPEN
  _onmessage: any = null;
  sent: string[] = [];
  constructor(public url: string) {
    (global as any).wsInstances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {}

  // Allow setting onmessage and then triggering it
  set onmessage(val: any) {
    this._onmessage = val;
  }
  get onmessage() {
    return this._onmessage;
  }

  receive(data: string) {
    if (this._onmessage) this._onmessage({ data });
  }
}

class MockTerminal {
  open() {}
  focus() {}
  dispose() {}
  loadAddon() {}
  onData() {
    return { dispose() {} };
  }
  onScroll() {
    return { dispose() {} };
  }
}

function makeWidget(container: HTMLElement, opts: Record<string, unknown> = {}) {
  const widget = document.createElement("uterm-session") as UtermSessionElement;
  widget.config = { workerId: "test-worker", ...opts };
  container.appendChild(widget);
  if (widget.connect) {
    widget.connect();
  }
  if (widget.isUpdatePending) {
    widget.performUpdate();
  }
  return widget;
}

describe("Command Approval UX", () => {
  let container: HTMLElement;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    (global as any).wsInstances = [];
    // Stub the browser globals per-test so afterEach can restore them — keeps
    // them from leaking into other test files that share this vitest worker.
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.stubGlobal("Terminal", MockTerminal);
    vi.stubGlobal("FitAddon", {
      FitAddon: class {
        fit() {}
      },
    });
  });

  afterEach(() => {
    document.body.removeChild(container);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows modal for admin in auto mode", async () => {
    const _hijack = makeWidget(container, { workerId: "w1", role: "admin", approvalUxMode: "auto" });
    const ws = (global as any).wsInstances[0];

    // Simulate approval_pending
    ws.receive(
      encodeControlFrame({
        type: "approval_pending",
        request_id: "req-1",
        command: "rm -rf /",
        expires_at: Date.now() / 1000 + 60,
      }),
    );

    // Need to await LitElement's update
    await new Promise((r) => setTimeout(r, 0));
    
    const prompt = container.querySelector("uterm-session")?.shadowRoot?.querySelector("uterm-approval-prompt");
    expect(prompt).toBeTruthy();
    const modal = prompt?.shadowRoot?.querySelector(".hijack-approval-modal");
    expect(modal).toBeTruthy();
    expect(modal?.textContent).toContain("rm -rf /");
    expect(modal?.querySelector(".hijack-btn-approve")).toBeTruthy();
  });

  it("shows statusbar for operator in auto mode", async () => {
    const _hijack = makeWidget(container, { workerId: "w1", role: "operator", approvalUxMode: "auto" });
    const ws = (global as any).wsInstances[0];

    ws.receive(
      encodeControlFrame({
        type: "approval_pending",
        request_id: "req-1",
        command: "rm -rf /",
        expires_at: Date.now() / 1000 + 60,
      }),
    );

    await new Promise((r) => setTimeout(r, 0));
    
    const prompt = container.querySelector("uterm-session")?.shadowRoot?.querySelector("uterm-approval-prompt");
    expect(prompt).toBeTruthy();
    const statusbar = prompt?.shadowRoot?.querySelector(".hijack-approval-statusbar");
    expect(statusbar).toBeTruthy();
    expect(prompt?.shadowRoot?.querySelector(".hijack-approval-modal")).toBeFalsy();
  });

  it("hides UI on approval_resolved", async () => {
    const _hijack = makeWidget(container, { workerId: "w1", role: "admin" });
    const ws = (global as any).wsInstances[0];

    ws.receive(
      encodeControlFrame({
        type: "approval_pending",
        request_id: "req-1",
        command: "rm",
        expires_at: Date.now() / 1000 + 60,
      }),
    );
    await new Promise((r) => setTimeout(r, 0));
    const prompt = container.querySelector("uterm-session")?.shadowRoot?.querySelector("uterm-approval-prompt");
    expect(prompt?.shadowRoot?.querySelector(".hijack-approval-modal")).toBeTruthy();

    ws.receive(encodeControlFrame({ type: "approval_resolved", outcome: "approved", request_id: "req-1" }));
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector("uterm-session")?.shadowRoot?.querySelector("uterm-approval-prompt")).toBeFalsy();
  });

  it("clears the approval countdown interval on dispose", async () => {
    const hijack = makeWidget(container, { workerId: "w1", role: "admin", approvalUxMode: "auto" });
    const ws = (global as any).wsInstances[0];

    ws.receive(
      encodeControlFrame({
        type: "approval_pending",
        request_id: "req-1",
        command: "rm",
        expires_at: Date.now() / 1000 + 60,
      }),
    );
    await new Promise((r) => setTimeout(r, 0));
    const prompt = container.querySelector("uterm-session")?.shadowRoot?.querySelector("uterm-approval-prompt") as any;
    expect(prompt?._timer).not.toBeNull();

    hijack.dispose();

    // dispose() must clear it, not leak a setInterval against the torn-down widget.
    expect(container.querySelector("uterm-session")?.shadowRoot?.querySelector("uterm-approval-prompt")).toBeFalsy();
    // pending approval should be unset
    expect((hijack as any)._pendingApproval).toBeNull();
  });
});
