//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/** Browser resume UX: hello.resumed → brief "Resumed" status flash. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { encodeControlFrame } from "./hijack-codec.js";
import type { UtermSessionElement } from "./session-element.js";
import "./session-element.js";

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: MockWebSocket[] = [];

  readyState = MockWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close"));
  }

  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  receive(data: string): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }
}

function flushLit(el: HTMLElement): void {
  const session = el as unknown as { isUpdatePending?: boolean; performUpdate?: () => void };
  if (session.isUpdatePending && session.performUpdate) {
    session.performUpdate();
  }
}

function statusText(widget: UtermSessionElement): string {
  flushLit(widget);
  return widget.shadowRoot?.querySelector("#statustext")?.textContent ?? "";
}

describe("session resume UI", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.useFakeTimers();
    vi.stubGlobal("sessionStorage", {
      getItem: vi.fn().mockReturnValue(null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("flashes Resumed status when hello.resumed is true", async () => {
    const widget = document.createElement("uterm-session") as UtermSessionElement;
    widget.config = { workerId: "resume-worker" };
    document.body.appendChild(widget);
    widget.connect();
    flushLit(widget);

    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    expect(ws).toBeTruthy();
    ws.open();

    ws.receive(
      encodeControlFrame({
        type: "hello",
        worker_online: true,
        resume_token: "tok-new",
        resumed: true,
        role: "operator",
      }),
    );
    flushLit(widget);

    expect(statusText(widget)).toContain("Resumed");

    vi.advanceTimersByTime(2500);
    flushLit(widget);
    // After flash timeout, status returns to a normal connected label.
    expect(statusText(widget)).not.toContain("Resumed");
    expect(statusText(widget).toLowerCase()).toMatch(/connected|watching|shared|hijacked/);
  });

  it("does not flash Resumed on a plain hello", () => {
    const widget = document.createElement("uterm-session") as UtermSessionElement;
    widget.config = { workerId: "plain-worker" };
    document.body.appendChild(widget);
    widget.connect();
    flushLit(widget);

    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    ws.open();
    ws.receive(
      encodeControlFrame({
        type: "hello",
        worker_online: true,
        resume_token: "tok",
        resumed: false,
      }),
    );
    flushLit(widget);

    expect(statusText(widget)).not.toContain("Resumed");
  });
});
