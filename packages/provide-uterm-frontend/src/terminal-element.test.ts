//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./terminal-element.js";
import type { TerminalElement } from "./terminal-element.js";

class MockFitAddon {
  fit(): void {}
  proposeDimensions(): { cols: number } {
    return { cols: 80 };
  }
}

class MockXterm {
  static instances: MockXterm[] = [];
  
  options: { fontSize: number; theme?: Record<string, unknown> } = { fontSize: 14 };
  buffer = {
    active: {
      baseY: 0,
      cursorY: 0,
      getLine: () => ({ translateToString: () => "" }),
    },
  };

  constructor() {
    MockXterm.instances.push(this);
  }

  open(_el: HTMLElement): void {}
  focus(): void {}
  write(_data: string): void {}
  dispose(): void {}
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  loadAddon(_a: any): void {}
  onData(_cb: (data: string) => void): { dispose(): void } {
    return { dispose: () => {} };
  }
  attachCustomKeyEventHandler(_cb: (e: KeyboardEvent) => boolean): void {}
}

beforeEach(() => {
  MockXterm.instances = [];
  vi.useFakeTimers();
  
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  (window as any).Terminal = MockXterm;
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  (window as any).FitAddon = { FitAddon: MockFitAddon };
  vi.stubGlobal("localStorage", {
    getItem: vi.fn().mockReturnValue(null),
    setItem: vi.fn(),
  });
  
  Object.defineProperty(document, "fonts", {
    value: { ready: Promise.resolve() },
    writable: true,
    configurable: true,
  });
  
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
    cb();
    return 0;
  });
  
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = "";
  vi.resetModules();
});

describe("TerminalElement", () => {
  it("attaches to the DOM and initializes xterm", async () => {
    const el = document.createElement("uterm-terminal") as TerminalElement;
    document.body.appendChild(el);
    
    // Lit components render asynchronously, wait for update
    await el.updateComplete;
    
    // Check that it rendered the DOM
    expect(el.querySelector(".terminal-frame")).toBeTruthy();
    expect(el.querySelector(".settings-panel")).toBeTruthy();
    
    // Check that xterm was initialized
    expect(MockXterm.instances.length).toBe(1);
  });

  it("exposes writeData and getBufferText", async () => {
    const el = document.createElement("uterm-terminal") as TerminalElement;
    document.body.appendChild(el);
    await el.updateComplete;
    
    expect(typeof el.writeData).toBe("function");
    expect(typeof el.getBufferText).toBe("function");
    
    // Should not throw
    el.writeData("test data");
  });

  it("updates status correctly", async () => {
    const el = document.createElement("uterm-terminal") as TerminalElement;
    document.body.appendChild(el);
    await el.updateComplete;
    
    el.connected = true;
    await el.updateComplete;
    
    const dots = el.querySelectorAll(".status-dot");
    expect(dots.length).toBeGreaterThan(0);
    dots.forEach((dot) => {
      expect(dot.classList.contains("connected")).toBe(true);
    });
  });
});
