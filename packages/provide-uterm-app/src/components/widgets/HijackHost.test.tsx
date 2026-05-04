//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTerminalStore } from "../../stores/terminalStore";
import { HijackHost } from "./HijackHost";

// Capture constructor calls from window.ProvideHijack
type CapturedCall = {
  container: HTMLElement;
  config: {
    workerId: string;
    showAnalysis?: boolean;
    mobileKeys?: boolean;
    onResize?: (cols: number, rows: number) => void;
  };
};

let calls: CapturedCall[] = [];

function mockHijackCtor() {
  calls = [];
  vi.stubGlobal(
    "ProvideHijack",
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    function (this: any, container: HTMLElement, config: CapturedCall["config"]) {
      calls.push({ container, config });
      this.sendControlMessage = () => {};
      this.terminalElement = null;
      // Production HijackHost cleanup calls widget.dispose() on unmount;
      // a missing dispose() crashes the teardown of every test that mounts.
      this.dispose = () => {};
    },
  );
}

function resetStore() {
  useTerminalStore.setState({ mounted: false, error: null, cols: 0, rows: 0 });
}

beforeEach(() => {
  calls = [];
  resetStore();
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetStore();
});

describe("HijackHost", () => {
  it("calls setMounted(false) when ProvideHijack is not loaded", () => {
    // window.ProvideHijack is undefined (not stubbed)
    render(<HijackHost sessionId="test-session" />);
    const { mounted, error } = useTerminalStore.getState();
    expect(mounted).toBe(false);
    expect(error).toContain("ProvideHijack is not available");
  });

  it("constructs the widget with workerId and onResize callback", () => {
    mockHijackCtor();
    render(<HijackHost sessionId="my-worker" />);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.config.workerId).toBe("my-worker");
    expect(typeof calls[0]?.config.onResize).toBe("function");
  });

  it("sets showAnalysis and mobileKeys false for non-operator surface", () => {
    mockHijackCtor();
    render(<HijackHost sessionId="s1" surface="user" />);
    expect(calls[0]?.config.showAnalysis).toBe(false);
    expect(calls[0]?.config.mobileKeys).toBe(false);
  });

  it("sets showAnalysis and mobileKeys true for operator surface", () => {
    mockHijackCtor();
    render(<HijackHost sessionId="s1" surface="operator" />);
    expect(calls[0]?.config.showAnalysis).toBe(true);
    expect(calls[0]?.config.mobileKeys).toBe(true);
  });

  it("calls setMounted(true) when widget is constructed", () => {
    mockHijackCtor();
    render(<HijackHost sessionId="s1" />);
    expect(useTerminalStore.getState().mounted).toBe(true);
  });

  it("installs a 15s keepalive interval for idle-prune protection", () => {
    mockHijackCtor();
    const spy = vi.spyOn(globalThis, "setInterval");
    render(<HijackHost sessionId="s1" />);
    // Exactly one interval, at 15s cadence — anything faster is a regression
    // to per-keystroke polling.
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0]?.[1]).toBe(15_000);
    spy.mockRestore();
  });

  it("calling onResize updates store dimensions", () => {
    mockHijackCtor();
    render(<HijackHost sessionId="s1" />);
    const onResize = calls[0]?.config.onResize;
    onResize?.(120, 40);
    const { cols, rows } = useTerminalStore.getState();
    expect(cols).toBe(120);
    expect(rows).toBe(40);
  });

  it("does not remount when deps change after first mount (mountedRef guard)", () => {
    mockHijackCtor();
    const { rerender } = render(<HijackHost sessionId="s1" />);
    expect(calls).toHaveLength(1);
    // Changing sessionId causes effect to re-run; mountedRef is already true → guard fires
    rerender(<HijackHost sessionId="s2" />);
    expect(calls).toHaveLength(1);
  });
});
