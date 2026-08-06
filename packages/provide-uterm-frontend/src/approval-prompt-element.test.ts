import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  type ApprovalPromptElement,
  registerApprovalPromptElement,
} from "./approval-prompt-element.js";

registerApprovalPromptElement();

describe("ApprovalPromptElement", () => {
  let element: ApprovalPromptElement;

  beforeEach(() => {
    element = document.createElement("uterm-approval-prompt");
    document.body.appendChild(element);
  });

  afterEach(() => {
    document.body.removeChild(element);
  });

  it("renders nothing when pendingApproval is null", () => {
    expect(element.shadowRoot?.querySelector(".hijack-approval-modal")).toBeFalsy();
    expect(element.shadowRoot?.querySelector(".hijack-approval-statusbar")).toBeFalsy();
  });

  it("renders modal when mode is modal and pendingApproval is set", async () => {
    element.mode = "modal";
    element.pendingApproval = { id: "1", command: "test", expiresAt: Date.now() / 1000 + 60 };
    await element.updateComplete;

    const root = element.shadowRoot;
    expect(root?.querySelector(".hijack-approval-modal")).toBeTruthy();
    expect(root?.querySelector(".hijack-approval-command")?.textContent).toBe("test");
    expect(root?.querySelector(".hijack-btn-approve")).toBeFalsy(); // isAdmin is false
  });

  it("renders admin actions when isAdmin is true", async () => {
    element.mode = "modal";
    element.isAdmin = true;
    element.pendingApproval = { id: "1", command: "test", expiresAt: Date.now() / 1000 + 60 };
    await element.updateComplete;

    const root = element.shadowRoot;
    expect(root?.querySelector(".hijack-btn-approve")).toBeTruthy();
    expect(root?.querySelector(".hijack-btn-reject")).toBeTruthy();
  });

  it("renders statusbar when mode is statusbar", async () => {
    element.mode = "statusbar";
    element.pendingApproval = { id: "1", command: "test", expiresAt: Date.now() / 1000 + 60 };
    await element.updateComplete;

    const root = element.shadowRoot;
    expect(root?.querySelector(".hijack-approval-statusbar")).toBeTruthy();
    expect(root?.querySelector(".hijack-approval-modal")).toBeFalsy();
  });

  it("fires approval-action event on approve", async () => {
    element.mode = "modal";
    element.isAdmin = true;
    element.pendingApproval = { id: "1", command: "test", expiresAt: Date.now() / 1000 + 60 };
    await element.updateComplete;

    let firedDetail = "";
    element.addEventListener("approval-action", (e: any) => {
      firedDetail = e.detail;
    });

    element.shadowRoot?.querySelector<HTMLButtonElement>(".hijack-btn-approve")?.click();
    expect(firedDetail).toBe("approve");
  });

  it("fires approval-expired when time runs out", async () => {
    let expiredFired = false;
    element.addEventListener("approval-expired", () => {
      expiredFired = true;
    });

    // Set expiration in the past
    element.pendingApproval = { id: "1", command: "test", expiresAt: Date.now() / 1000 - 10 };
    await element.updateComplete;

    // wait for interval to tick once
    await new Promise((r) => setTimeout(r, 1100));
    expect(expiredFired).toBe(true);
  });
});
