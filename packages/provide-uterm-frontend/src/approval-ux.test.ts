
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProvideHijack } from "./hijack.js";
import { encodeControlFrame } from "./hijack-codec.js";

// Reuse mocks logic
class MockWebSocket {
  readyState = 1; // OPEN
  _onmessage: any = null;
  sent: string[] = [];
  constructor(public url: string) { (global as any).wsInstances.push(this); }
  send(d: string) { this.sent.push(d); }
  close() {}

  // Allow setting onmessage and then triggering it
  set onmessage(val: any) { this._onmessage = val; }
  get onmessage() { return this._onmessage; }

  receive(data: string) {
    if (this._onmessage) this._onmessage({ data });
  }
}

(global as any).wsInstances = [];
(window as any).WebSocket = MockWebSocket as any;
(window as any).Terminal = class { 
  open() {} 
  focus() {} 
  dispose() {} 
  loadAddon() {}
  onData() { return { dispose() {} }; }
  onScroll() { return { dispose() {} }; }
};
(window as any).FitAddon = { FitAddon: class { fit() {} } };

describe("Command Approval UX", () => {
  let container: HTMLElement;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    (global as any).wsInstances = [];
  });

  afterEach(() => {
    document.body.removeChild(container);
    vi.restoreAllMocks();
  });

  it("shows modal for admin in auto mode", async () => {
    const hijack = new ProvideHijack(container, { workerId: "w1", role: "admin", approvalUxMode: "auto" });
    const ws = (global as any).wsInstances[0];
    
    // Simulate approval_pending
    ws.receive(encodeControlFrame({ 
      type: "approval_pending", 
      request_id: "req-1", 
      command: "rm -rf /",
      expires_at: Date.now() / 1000 + 60
    }));

    const modal = container.querySelector(".hijack-approval-modal");
    expect(modal).toBeTruthy();
    expect(modal?.textContent).toContain("rm -rf /");
    expect(modal?.querySelector(".hijack-btn-approve")).toBeTruthy();
  });

  it("shows statusbar for operator in auto mode", async () => {
    const hijack = new ProvideHijack(container, { workerId: "w1", role: "operator", approvalUxMode: "auto" });
    const ws = (global as any).wsInstances[0];
    
    ws.receive(encodeControlFrame({ 
      type: "approval_pending", 
      request_id: "req-1", 
      command: "rm -rf /",
      expires_at: Date.now() / 1000 + 60
    }));

    const statusbar = container.querySelector(".hijack-approval-statusbar");
    expect(statusbar).toBeTruthy();
    expect(container.querySelector(".hijack-approval-modal")).toBeFalsy();
  });

  it("hides UI on approval_resolved", async () => {
    const hijack = new ProvideHijack(container, { workerId: "w1", role: "admin" });
    const ws = (global as any).wsInstances[0];

    ws.receive(encodeControlFrame({ type: "approval_pending", request_id: "req-1", command: "rm", expires_at: Date.now() / 1000 + 60 }));
    expect(container.querySelector(".hijack-approval-modal")).toBeTruthy();

    ws.receive(encodeControlFrame({ type: "approval_resolved", outcome: "approved", request_id: "req-1" }));
    expect(container.querySelector(".hijack-approval-modal")).toBeFalsy();
  });

  it("clears the approval countdown interval on dispose", async () => {
    const hijack = new ProvideHijack(container, { workerId: "w1", role: "admin", approvalUxMode: "auto" });
    const ws = (global as any).wsInstances[0];

    ws.receive(encodeControlFrame({ type: "approval_pending", request_id: "req-1", command: "rm", expires_at: Date.now() / 1000 + 60 }));
    // The countdown setInterval is now live.
    expect((hijack as any)._approvalTimer).not.toBeNull();

    hijack.dispose();

    // dispose() must clear it, not leak a setInterval against the torn-down widget.
    expect((hijack as any)._approvalTimer).toBeNull();
    expect((hijack as any)._pendingApproval).toBeNull();
  });
});
