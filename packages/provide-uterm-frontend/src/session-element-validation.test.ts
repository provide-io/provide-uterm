//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it } from "vitest";
import { UtermSessionElement } from "./session-element.js";

type SessionInternals = {
  _handleMessage: (message: unknown) => void;
  _hijackState: { canHijack: boolean; hijacked: boolean; inputMode: string };
  _pendingApproval: unknown;
};

let element: UtermSessionElement | null = null;
afterEach(() => {
  element?.remove();
  element = null;
});

function makeSession(): SessionInternals {
  element = document.createElement("uterm-session") as UtermSessionElement;
  element.config = { workerId: "validation" };
  document.body.appendChild(element);
  return element as unknown as SessionInternals;
}

describe("session control-frame validation", () => {
  it("rejects malformed hello payloads without mutating session capabilities", () => {
    const session = makeSession();
    const before = session._hijackState.canHijack;
    for (const frame of [
      [],
      Object.assign([], { type: "hello" }),
      { type: "other" },
      { type: "hello", can_hijack: "yes" },
      { type: "hello", hijacked: 1 },
      { type: "hello", resumed: "yes" },
      { type: "hello", input_mode: 1 },
      { type: "hello", role: true },
      { type: "hello", capabilities: [] },
    ]) {
      session._handleMessage(frame);
    }
    expect(session._hijackState.canHijack).toBe(before);
  });

  it("accepts nullable optional hello fields and object capabilities", () => {
    const session = makeSession();
    session._handleMessage({
      type: "hello",
      can_hijack: true,
      hijacked: false,
      hijacked_by_me: null,
      worker_online: true,
      input_mode: "open",
      role: null,
      capabilities: { hijack_control: "rest", hijack_step_supported: false },
    });
    expect(session._hijackState).toMatchObject({ canHijack: true, hijacked: false, inputMode: "open" });
  });

  it("rejects malformed hijack-state payloads and accepts the complete contract", () => {
    const session = makeSession();
    for (const frame of [
      { type: "hijack_state" },
      { type: "hijack_state", hijacked: "yes" },
      { type: "hijack_state", hijacked: true, owner: 1 },
      { type: "hijack_state", hijacked: true, input_mode: 1 },
    ]) {
      session._handleMessage(frame);
    }
    expect(session._hijackState.hijacked).toBe(false);
    session._handleMessage({ type: "hijack_state", hijacked: true, owner: "other", input_mode: "hijack" });
    expect(session._hijackState.hijacked).toBe(true);
  });

  it("rejects malformed approval requests and stores a finite complete request", () => {
    const session = makeSession();
    for (const frame of [
      { type: "approval_pending" },
      { type: "approval_pending", request_id: 1, command: "ls", expires_at: 1 },
      { type: "approval_pending", request_id: "r", command: 1, expires_at: 1 },
      { type: "approval_pending", request_id: "r", command: "ls", expires_at: "later" },
      { type: "approval_pending", request_id: "r", command: "ls", expires_at: Number.POSITIVE_INFINITY },
    ]) {
      session._handleMessage(frame);
    }
    expect(session._pendingApproval).toBeNull();
    session._handleMessage({ type: "approval_pending", request_id: "r", command: "ls", expires_at: 10 });
    expect(session._pendingApproval).toEqual({ id: "r", command: "ls", expiresAt: 10 });
    session._handleMessage({ type: "approval_resolved" });
    expect(session._pendingApproval).toBeNull();
  });

  it("keeps disconnected control actions side-effect free", async () => {
    const session = makeSession() as SessionInternals & {
      _doHijack: () => void;
      _doStep: () => void;
      _doRelease: () => void;
      _doResync: () => void;
      _doAnalyze: () => void;
      _sendInput: () => void;
      _sendMkey: (data: string) => void;
      _handleApprovalAction: (event: CustomEvent) => Promise<void>;
    };
    session._doHijack();
    session._doStep();
    session._doRelease();
    session._doResync();
    session._doAnalyze();
    session._sendInput();
    session._sendMkey("x");
    await session._handleApprovalAction(new CustomEvent("approval-action", { detail: "approve" }));
    expect(session._hijackState.hijacked).toBe(false);

    const detached = new UtermSessionElement();
    expect(detached.terminal).toBeNull();
    expect(() => detached.disconnect()).not.toThrow();
    expect(() => detached.dispose()).not.toThrow();
  });
});
