//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { encodeControlFrame } from "../../utils/controlFrames";
import { useInspectStore } from "../../stores/inspectStore";
import { useInspectWs } from "./useInspectWs";

interface FakeSocket {
  url: string;
  readyState: number;
  listeners: Record<string, Array<(ev: { data?: unknown } | undefined) => void>>;
  send: (data: string) => void;
  close: () => void;
  addEventListener: (name: string, fn: (ev: { data?: unknown } | undefined) => void) => void;
  emit: (name: string, ev?: { data?: unknown }) => void;
}

let lastSocket: FakeSocket | null = null;

class MockWebSocket implements FakeSocket {
  static OPEN = 1;
  url: string;
  readyState = MockWebSocket.OPEN;
  listeners: Record<string, Array<(ev: { data?: unknown } | undefined) => void>> = {};
  sent: string[] = [];
  closed = false;

  constructor(url: string) {
    this.url = url;
    lastSocket = this;
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
  }

  addEventListener(name: string, fn: (ev: { data?: unknown } | undefined) => void) {
    if (!this.listeners[name]) this.listeners[name] = [];
    this.listeners[name].push(fn);
  }

  emit(name: string, ev?: { data?: unknown }) {
    for (const fn of this.listeners[name] ?? []) fn(ev);
  }
}

const ORIGINAL_WS = globalThis.WebSocket;
const ORIGINAL_LOCATION = window.location;

beforeEach(() => {
  lastSocket = null;
  (globalThis as unknown as { WebSocket: typeof MockWebSocket }).WebSocket = MockWebSocket;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { protocol: "http:", host: "example.test", search: "" },
  });
  useInspectStore.getState().clear();
});

afterEach(() => {
  (globalThis as unknown as { WebSocket: typeof ORIGINAL_WS }).WebSocket = ORIGINAL_WS;
  Object.defineProperty(window, "location", { configurable: true, value: ORIGINAL_LOCATION });
  vi.restoreAllMocks();
});

function makeHttpFrame(payload: Record<string, unknown>): string {
  return encodeControlFrame({ _channel: "http", ...payload });
}

describe("useInspectWs", () => {
  it("connects and updates status", () => {
    renderHook(() => useInspectWs("s1"));
    expect(lastSocket).not.toBeNull();
    expect(lastSocket?.url).toContain("ws://example.test/ws/browser/s1/term");
    act(() => lastSocket?.emit("open"));
    expect(useInspectStore.getState().wsStatus).toBe("connected");
    act(() => lastSocket?.emit("close"));
    expect(useInspectStore.getState().wsStatus).toBe("disconnected");
  });

  it("uses wss:// for https pages without query tokens", () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { protocol: "https:", host: "example.test", search: "?token=abc" },
    });
    renderHook(() => useInspectWs("s1"));
    expect(lastSocket?.url).toBe("wss://example.test/ws/browser/s1/term");
  });

  it("ignores non-string frame payloads", () => {
    renderHook(() => useInspectWs("s1"));
    act(() => lastSocket?.emit("message", { data: 42 as unknown as string }));
    expect(useInspectStore.getState().exchanges).toHaveLength(0);
  });

  it("ignores frames without http channel", () => {
    renderHook(() => useInspectWs("s1"));
    const frame = encodeControlFrame({ _channel: "other", type: "http_req" });
    act(() => lastSocket?.emit("message", { data: frame }));
    expect(useInspectStore.getState().exchanges).toHaveLength(0);
  });

  it("adds a valid http_req frame", () => {
    renderHook(() => useInspectWs("s1"));
    const frame = makeHttpFrame({
      type: "http_req",
      id: "r1",
      ts: 1,
      method: "GET",
      url: "/x",
      headers: {},
      body_size: 0,
    });
    act(() => lastSocket?.emit("message", { data: frame }));
    expect(useInspectStore.getState().exchanges).toHaveLength(1);
  });

  it("adds a valid http_res frame", () => {
    renderHook(() => useInspectWs("s1"));
    const reqFrame = makeHttpFrame({
      type: "http_req",
      id: "r1",
      ts: 1,
      method: "GET",
      url: "/x",
      headers: {},
      body_size: 0,
    });
    const resFrame = makeHttpFrame({
      type: "http_res",
      id: "r1",
      ts: 2,
      status: 200,
      status_text: "OK",
      headers: {},
      body_size: 0,
      duration_ms: 1,
    });
    act(() => {
      lastSocket?.emit("message", { data: reqFrame });
      lastSocket?.emit("message", { data: resFrame });
    });
    expect(useInspectStore.getState().exchanges[0]?.response?.status).toBe(200);
  });

  it("rejects malformed http_req without throwing and logs warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    renderHook(() => useInspectWs("s1"));
    const bad = makeHttpFrame({ type: "http_req", id: 7, ts: 1, method: "GET", url: "/x", headers: {}, body_size: 0 });
    act(() => lastSocket?.emit("message", { data: bad }));
    expect(useInspectStore.getState().exchanges).toHaveLength(0);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("http_req"));
  });

  it("rejects malformed http_res", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    renderHook(() => useInspectWs("s1"));
    const bad = makeHttpFrame({ type: "http_res", id: "r1", ts: 1, status: "200", status_text: "OK", headers: {}, body_size: 0, duration_ms: 1 });
    act(() => lastSocket?.emit("message", { data: bad }));
    expect(warn).toHaveBeenCalled();
  });

  it("forwards http_intercept_state to syncInterceptState", () => {
    renderHook(() => useInspectWs("s1"));
    const frame = makeHttpFrame({
      type: "http_intercept_state",
      inspect_enabled: true,
      enabled: true,
      timeout_s: 10,
      timeout_action: "drop",
    });
    act(() => lastSocket?.emit("message", { data: frame }));
    expect(useInspectStore.getState().interceptEnabled).toBe(true);
    expect(useInspectStore.getState().interceptTimeout).toBe(10);
  });

  it("rejects malformed http_intercept_state and keeps prior state", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    renderHook(() => useInspectWs("s1"));
    const frame = makeHttpFrame({
      type: "http_intercept_state",
      inspect_enabled: true,
      enabled: "false",
      timeout_s: Infinity,
      timeout_action: "drop",
    });
    act(() => lastSocket?.emit("message", { data: frame }));
    expect(useInspectStore.getState().interceptEnabled).toBe(false);
    expect(useInspectStore.getState().interceptTimeout).toBe(30);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("http_intercept_state"));
  });

  it("resets decoder state after malformed control frames so later frames are accepted", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    renderHook(() => useInspectWs("s1"));
    act(() => lastSocket?.emit("message", { data: "\x10\x02zzzzzzzz:{}" }));
    const frame = makeHttpFrame({
      type: "http_req",
      id: "r1",
      ts: 1,
      method: "GET",
      url: "/x",
      headers: {},
      body_size: 0,
    });
    act(() => lastSocket?.emit("message", { data: frame }));
    expect(useInspectStore.getState().exchanges).toHaveLength(1);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("malformed control frame"));
  });

  it("returns a sendJson that posts when socket is open", () => {
    const { result } = renderHook(() => useInspectWs("s1"));
    result.current.sendJson({ a: 1 });
    expect((lastSocket as unknown as MockWebSocket).sent).toEqual([JSON.stringify({ a: 1 })]);
  });

  it("sendJson is a noop when socket not open", () => {
    const { result } = renderHook(() => useInspectWs("s1"));
    (lastSocket as unknown as MockWebSocket).readyState = 0;
    result.current.sendJson({ b: 2 });
    expect((lastSocket as unknown as MockWebSocket).sent).toEqual([]);
  });

  it("re-throws non-validation errors raised in handlers", () => {
    const boom = new Error("boom");
    vi.spyOn(useInspectStore.getState(), "syncInterceptState").mockImplementation(() => {
      throw boom;
    });
    renderHook(() => useInspectWs("s1"));
    const frame = makeHttpFrame({
      type: "http_intercept_state",
      inspect_enabled: true,
      enabled: false,
      timeout_s: 1,
      timeout_action: "x",
    });
    expect(() => lastSocket?.emit("message", { data: frame })).toThrow(boom);
  });

  it("closes the websocket on unmount", () => {
    const { unmount } = renderHook(() => useInspectWs("s1"));
    const sock = lastSocket as unknown as MockWebSocket;
    unmount();
    expect(sock.closed).toBe(true);
  });
});
