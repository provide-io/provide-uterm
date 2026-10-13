//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/** Public connection and close events on `<uterm-session>` and `<uterm-terminal>`. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  FINAL_CLOSE_CODES,
  UTERM_CLOSE_EVENT,
  UTERM_CONNECTION_EVENT,
  type UtermCloseEventDetail,
  type UtermConnectionEventDetail,
  VIEWER_REVOKED_CLOSE_CODE,
  isFinalCloseCode,
} from "./connection-events.js";
import { type UtermSessionElement, registerUtermSessionElement } from "./session-element.js";
import { type TerminalElement, registerTerminalElement } from "./terminal-element.js";
import * as publicEntry from "./public.js";

registerUtermSessionElement();
registerTerminalElement();

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

  /** A close this side asked for. Fires synchronously, which is harsher than a browser. */
  close(): void {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close", { code: 1005, reason: "", wasClean: true }));
  }

  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  /** A close the server sent (or a drop, with 1006). */
  serverClose(code: number, reason = "", wasClean = true): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close", { code, reason, wasClean }));
  }
}

function sockets(): MockWebSocket[] {
  return MockWebSocket.instances;
}

function latest(): MockWebSocket {
  const ws = sockets()[sockets().length - 1];
  if (!ws) throw new Error("no socket opened");
  return ws;
}

interface Recorded {
  connection: UtermConnectionEventDetail[];
  close: UtermCloseEventDetail[];
  closeEvents: CustomEvent<UtermCloseEventDetail>[];
}

/** Listen on the document, so the events must bubble out of the element (and its shadow root). */
function record(signal: AbortSignal): Recorded {
  const rec: Recorded = { connection: [], close: [], closeEvents: [] };
  document.addEventListener(
    UTERM_CONNECTION_EVENT,
    (e) => {
      rec.connection.push((e as CustomEvent<UtermConnectionEventDetail>).detail);
    },
    { signal },
  );
  document.addEventListener(
    UTERM_CLOSE_EVENT,
    (e) => {
      const ev = e as CustomEvent<UtermCloseEventDetail>;
      rec.close.push(ev.detail);
      rec.closeEvents.push(ev);
    },
    { signal },
  );
  return rec;
}

function mountSession(): UtermSessionElement {
  const el = document.createElement("uterm-session") as UtermSessionElement;
  el.config = { workerId: "events-worker" };
  document.body.appendChild(el);
  return el;
}

function mountTerminal(): TerminalElement {
  const el = document.createElement("uterm-terminal") as TerminalElement;
  el.config = { wsUrl: "ws://localhost/ws/terminal" };
  document.body.appendChild(el);
  return el;
}

let rec: Recorded;
let controller: AbortController;

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  vi.useFakeTimers();
  vi.stubGlobal("sessionStorage", {
    getItem: vi.fn().mockReturnValue(null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  });
  controller = new AbortController();
  rec = record(controller.signal);
});

afterEach(() => {
  controller.abort();
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("connection-events module", () => {
  it("treats only 4410 (viewer access revoked) as final", () => {
    expect(VIEWER_REVOKED_CLOSE_CODE).toBe(4410);
    expect([...FINAL_CLOSE_CODES]).toEqual([4410]);
    expect(isFinalCloseCode(4410)).toBe(true);
    for (const code of [1000, 1001, 1006, 1008, 1011, 4401, 4403]) {
      expect(isFinalCloseCode(code)).toBe(false);
    }
  });

  it("is exported from the package entry", () => {
    expect(publicEntry.UTERM_CONNECTION_EVENT).toBe("uterm:connection");
    expect(publicEntry.UTERM_CLOSE_EVENT).toBe("uterm:close");
    expect(publicEntry.VIEWER_REVOKED_CLOSE_CODE).toBe(4410);
    expect(publicEntry.isFinalCloseCode(4410)).toBe(true);
  });
});

describe("<uterm-session> connection events", () => {
  it("reports connected=false until a socket opens, then true", () => {
    const el = mountSession();
    expect(el.connected).toBe(false);
    el.connect();
    expect(el.connected).toBe(false);
    latest().open();
    expect(el.connected).toBe(true);
    expect(rec.connection).toEqual([{ connected: true }]);
  });

  it("fires bubbling, composed events from the element", () => {
    const el = mountSession();
    const seen: Event[] = [];
    el.addEventListener(UTERM_CONNECTION_EVENT, (e) => seen.push(e));
    el.addEventListener(UTERM_CLOSE_EVENT, (e) => seen.push(e));
    el.connect();
    latest().open();
    latest().serverClose(1011, "boom");
    expect(seen.map((e) => e.type)).toEqual(["uterm:connection", "uterm:connection", "uterm:close"]);
    for (const e of seen) {
      expect(e.bubbles).toBe(true);
      expect(e.composed).toBe(true);
      expect(e.target).toBe(el);
    }
    expect(seen[2]?.cancelable).toBe(true);
  });

  it("fires disconnect and close (with code and reason) before scheduling a reconnect", () => {
    const el = mountSession();
    el.connect();
    latest().open();
    let reconnectPendingDuringClose: boolean | null = null;
    el.addEventListener(UTERM_CLOSE_EVENT, () => {
      reconnectPendingDuringClose = (el as unknown as { _hijackState: { reconnectTimer: unknown } })._hijackState
        .reconnectTimer !== null;
      expect(el.connected).toBe(false);
    });
    latest().serverClose(1011, "server restart", false);
    expect(rec.connection).toEqual([{ connected: true }, { connected: false }]);
    expect(rec.close).toEqual([{ code: 1011, reason: "server restart", wasClean: false, willReconnect: true }]);
    expect(reconnectPendingDuringClose).toBe(false);
    expect(sockets()).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    expect(sockets()).toHaveLength(2);
  });

  it("fires the events for sockets it reopens on its own", () => {
    const el = mountSession();
    el.connect();
    latest().open();
    latest().serverClose(1006, "", false);
    vi.advanceTimersByTime(1000);
    expect(sockets()).toHaveLength(2);
    latest().open();
    expect(el.connected).toBe(true);
    latest().serverClose(1001, "going away");
    vi.advanceTimersByTime(2000);
    expect(sockets()).toHaveLength(3);
    latest().open();
    expect(rec.connection).toEqual([
      { connected: true },
      { connected: false },
      { connected: true },
      { connected: false },
      { connected: true },
    ]);
    expect(rec.close.map((d) => d.code)).toEqual([1006, 1001]);
  });

  it("fires close for a socket that never opened, without a connection change", () => {
    const el = mountSession();
    el.connect();
    latest().serverClose(1006, "", false);
    expect(rec.connection).toEqual([]);
    expect(rec.close).toEqual([{ code: 1006, reason: "", wasClean: false, willReconnect: true }]);
    expect(el.connected).toBe(false);
  });

  it("does not reconnect when a listener calls preventDefault()", () => {
    const el = mountSession();
    el.addEventListener(UTERM_CLOSE_EVENT, (e) => e.preventDefault());
    el.connect();
    latest().open();
    latest().serverClose(4000, "app says stop");
    expect(rec.closeEvents[0]?.defaultPrevented).toBe(true);
    vi.advanceTimersByTime(120_000);
    expect(sockets()).toHaveLength(1);
    expect(el.connected).toBe(false);
  });

  it("never reconnects after 4410, even with no listener", () => {
    const el = mountSession();
    el.connect();
    latest().open();
    latest().serverClose(4410, "viewer access revoked");
    expect(rec.close).toEqual([
      { code: 4410, reason: "viewer access revoked", wasClean: true, willReconnect: false },
    ]);
    vi.advanceTimersByTime(120_000);
    expect(sockets()).toHaveLength(1);
    const lit = el as unknown as { isUpdatePending: boolean; performUpdate(): void };
    if (lit.isUpdatePending) lit.performUpdate();
    expect(el.shadowRoot?.querySelector("#statustext")?.textContent).toContain("Access revoked");
  });

  it("can still be reconnected by hand after a final close", () => {
    const el = mountSession();
    el.connect();
    latest().open();
    latest().serverClose(4410, "viewer access revoked");
    el.connect();
    expect(sockets()).toHaveLength(2);
    latest().open();
    expect(el.connected).toBe(true);
  });

  it("disconnect() reports the drop once and fires no close event", () => {
    const el = mountSession();
    el.connect();
    latest().open();
    el.disconnect();
    expect(el.connected).toBe(false);
    expect(rec.connection).toEqual([{ connected: true }, { connected: false }]);
    expect(rec.close).toEqual([]);
    vi.advanceTimersByTime(120_000);
    expect(sockets()).toHaveLength(1);
  });

  it("a manual reconnect over an open socket reports the drop and the new connection", () => {
    const el = mountSession();
    el.connect();
    latest().open();
    el.connect();
    expect(sockets()).toHaveLength(2);
    expect(el.connected).toBe(false);
    latest().open();
    expect(rec.connection).toEqual([{ connected: true }, { connected: false }, { connected: true }]);
    expect(rec.close).toEqual([]);
    vi.advanceTimersByTime(120_000);
    expect(sockets()).toHaveLength(2);
  });
});

describe("<uterm-terminal> connection events", () => {
  beforeEach(() => {
    // The element builds xterm on first render; give it inert stand-ins.
    const term = {
      options: { fontSize: 14 },
      buffer: { active: { baseY: 0, cursorY: 0, getLine: () => undefined } },
      open() {},
      focus() {},
      write() {},
      dispose() {},
      loadAddon() {},
      onData() {},
      attachCustomKeyEventHandler() {},
    };
    vi.stubGlobal("Terminal", function MockXterm() {
      return term;
    });
    vi.stubGlobal("FitAddon", {
      FitAddon: function MockFit() {
        return { fit() {}, proposeDimensions: () => ({ cols: 80 }) };
      },
    });
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      disconnect() {}
    });
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    Object.defineProperty(document, "fonts", {
      value: { ready: Promise.resolve() },
      writable: true,
      configurable: true,
    });
  });

  it("fires connect, disconnect and close, then reconnects", () => {
    const el = mountTerminal();
    el.connect();
    expect(el.connected).toBe(false);
    latest().open();
    expect(el.connected).toBe(true);
    latest().serverClose(1011, "boom", false);
    expect(el.connected).toBe(false);
    expect(rec.connection).toEqual([{ connected: true }, { connected: false }]);
    expect(rec.close).toEqual([{ code: 1011, reason: "boom", wasClean: false, willReconnect: true }]);
    vi.advanceTimersByTime(1000);
    expect(sockets()).toHaveLength(2);
    latest().open();
    expect(rec.connection.at(-1)).toEqual({ connected: true });
  });

  it("does not reconnect when a listener calls preventDefault()", () => {
    const el = mountTerminal();
    el.addEventListener(UTERM_CLOSE_EVENT, (e) => e.preventDefault());
    el.connect();
    latest().open();
    latest().serverClose(1001);
    vi.advanceTimersByTime(60_000);
    expect(sockets()).toHaveLength(1);
  });

  it("never reconnects after 4410", () => {
    const el = mountTerminal();
    el.connect();
    latest().open();
    latest().serverClose(4410, "viewer access revoked");
    expect(rec.close[0]?.willReconnect).toBe(false);
    vi.advanceTimersByTime(60_000);
    expect(sockets()).toHaveLength(1);
    expect(el.connected).toBe(false);
  });

  it("disconnect() reports the drop and fires no close event", () => {
    const el = mountTerminal();
    el.connect();
    latest().open();
    el.disconnect();
    expect(el.connected).toBe(false);
    expect(rec.connection).toEqual([{ connected: true }, { connected: false }]);
    expect(rec.close).toEqual([]);
  });
});
