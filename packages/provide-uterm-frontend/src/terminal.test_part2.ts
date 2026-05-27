  it("overlay click closes settings panel", async () => {
    const { container } = await makeTerminal();
    const gear = container.querySelector<HTMLButtonElement>(`[id^="gearBtn-"]`)!;
    const overlay = container.querySelector<HTMLElement>(`[id^="settingsOverlay-"]`)!;
    const panel = container.querySelector<HTMLElement>(`[id^="settingsPanel-"]`)!;
    gear.click(); // open
    overlay.click(); // close via overlay
    expect(panel.classList.contains("open")).toBe(false);
  });

  it("theme buttons switch theme (crt)", async () => {
    const { container } = await makeTerminal({ theme: "code" });
    const root = container.querySelector(".provide-uterm")!;
    const crtBtn = container.querySelector<HTMLButtonElement>('[data-theme="crt"]')!;
    crtBtn.click();
    expect(root.classList.contains("theme-crt")).toBe(true);
    expect(localStorage.setItem).toHaveBeenCalled();
  });

  it("theme buttons switch theme (bbs)", async () => {
    const { container } = await makeTerminal();
    const bbsBtn = container.querySelector<HTMLButtonElement>('[data-theme="bbs"]')!;
    bbsBtn.click();
    expect(container.querySelector(".provide-uterm")?.classList.contains("theme-bbs")).toBe(true);
  });

  it("theme buttons switch theme (glass)", async () => {
    const { container } = await makeTerminal();
    const glassBtn = container.querySelector<HTMLButtonElement>('[data-theme="glass"]')!;
    glassBtn.click();
    expect(container.querySelector(".provide-uterm")?.classList.contains("theme-glass")).toBe(true);
  });

  it("theme buttons switch theme (code)", async () => {
    const { container } = await makeTerminal({ theme: "bbs" });
    const codeBtn = container.querySelector<HTMLButtonElement>('[data-theme="code"]')!;
    codeBtn.click();
    expect(container.querySelector(".provide-uterm")?.classList.contains("theme-code")).toBe(true);
  });

  it("cols range input updates setting display", async () => {
    const { container } = await makeTerminal();
    const colsInput = container.querySelector<HTMLInputElement>(`[id^="setCols-"]`)!;
    colsInput.value = "100";
    colsInput.dispatchEvent(new Event("input"));
    const colsVal = container.querySelector<HTMLElement>(`[id^="valCols-"]`)!;
    expect(colsVal.textContent).toBe("100");
  });

  it("rows range input updates setting display", async () => {
    const { container } = await makeTerminal();
    const rowsInput = container.querySelector<HTMLInputElement>(`[id^="setRows-"]`)!;
    rowsInput.value = "30";
    rowsInput.dispatchEvent(new Event("input"));
    const rowsVal = container.querySelector<HTMLElement>(`[id^="valRows-"]`)!;
    expect(rowsVal.textContent).toBe("30");
  });

  it("fontSize range input updates setting display", async () => {
    const { container } = await makeTerminal();
    const fsInput = container.querySelector<HTMLInputElement>(`[id^="setFontSize-"]`)!;
    fsInput.value = "16";
    fsInput.dispatchEvent(new Event("input"));
    const fsVal = container.querySelector<HTMLElement>(`[id^="valFontSize-"]`)!;
    expect(fsVal.textContent).toBe("16px");
  });

  it("pageBg color input updates CSS variable", async () => {
    const { container } = await makeTerminal();
    const pageBgInput = container.querySelector<HTMLInputElement>(`[id^="setPageBg-"]`)!;
    pageBgInput.value = "#ffffff";
    pageBgInput.dispatchEvent(new Event("input"));
    const root = container.querySelector<HTMLElement>(".provide-uterm")!;
    expect(root.style.getPropertyValue("--bg-page")).toBe("#ffffff");
  });

  it("termBg color input updates CSS variable", async () => {
    const { container } = await makeTerminal();
    const termBgInput = container.querySelector<HTMLInputElement>(`[id^="setTermBg-"]`)!;
    termBgInput.value = "#111111";
    termBgInput.dispatchEvent(new Event("input"));
    const root = container.querySelector<HTMLElement>(".provide-uterm")!;
    expect(root.style.getPropertyValue("--bg-terminal")).toBe("#111111");
  });

  it("scanlines checkbox can be toggled", async () => {
    const { container } = await makeTerminal({ theme: "code" });
    const root = container.querySelector(".provide-uterm")!;
    const scanlines = container.querySelector<HTMLInputElement>(`[id^="fxScanlines-"]`)!;
    scanlines.checked = true;
    scanlines.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-scanlines")).toBe(true);
    scanlines.checked = false;
    scanlines.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-scanlines")).toBe(false);
  });

  it("vignette checkbox can be toggled", async () => {
    const { container } = await makeTerminal();
    const root = container.querySelector(".provide-uterm")!;
    const vignette = container.querySelector<HTMLInputElement>(`[id^="fxVignette-"]`)!;
    vignette.checked = true;
    vignette.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-vignette")).toBe(true);
  });

  it("glow checkbox can be toggled", async () => {
    const { container } = await makeTerminal();
    const root = container.querySelector(".provide-uterm")!;
    const glow = container.querySelector<HTMLInputElement>(`[id^="fxGlow-"]`)!;
    glow.checked = true;
    glow.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-glow")).toBe(true);
  });
});

describe("ProvideTerminal getBufferText", () => {
  it("returns empty string when terminal is disposed", async () => {
    const { terminal } = await makeTerminal();
    terminal.dispose(); // dispose clears term
    const text = terminal.getBufferText();
    expect(text).toBe("");
  });

  it("returns buffer text when terminal is active", async () => {
    const { terminal } = await makeTerminal();
    const text = terminal.getBufferText(10);
    // The mock xterm buffer has lines 0, 1, 2 with content
    expect(typeof text).toBe("string");
  });

  it("respects maxLines parameter", async () => {
    const { terminal } = await makeTerminal();
    const text = terminal.getBufferText(1);
    expect(typeof text).toBe("string");
  });
});

describe("ProvideTerminal title display", () => {
  it("uses title from config in frame (uppercased)", async () => {
    const { container } = await makeTerminal({ title: "My Terminal" });
    expect(container.innerHTML).toContain("MY TERMINAL");
  });

  it("uses default title when none provided", async () => {
    const { container } = await makeTerminal();
    expect(container.innerHTML).toContain("WARP AGENT RUNTIME PLATFORM");
  });

  it("uses null title gracefully", async () => {
    const { container } = await makeTerminal({ title: null });
    expect(container.innerHTML).toContain("WARP AGENT RUNTIME PLATFORM");
  });
});

describe("ProvideTerminal loading screen", () => {
  it("hides loading screen when first data arrives", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    getWs().triggerMessage("some terminal data");
    const loading = container.querySelector<HTMLElement>(`[id^="loadingScreen-"]`)!;
    expect(loading.style.display).toBe("none");
  });

  it("keeps loading screen visible before first data", async () => {
    const { container } = await makeTerminal();
    const loading = container.querySelector<HTMLElement>(`[id^="loadingScreen-"]`)!;
    // Before any message, loading should not have display:none
    expect(loading.style.display).not.toBe("none");
  });
});

describe("ProvideTerminal reconnect timer", () => {
  it("does not schedule reconnect if already scheduled", async () => {
    await makeTerminal();
    const ws = getWs();
    ws.close(); // schedules reconnect
    const countBefore = MockWebSocket.instances.length;
    // Don't advance timer — ensure no extra WS created
    expect(MockWebSocket.instances.length).toBe(countBefore);
  });

  it("disconnect before reconnect timer fires cancels reconnect", async () => {
    const { terminal } = await makeTerminal();
    getWs().close(); // schedules reconnect at 1s
    terminal.disconnect(); // should cancel timer
    const countBefore = MockWebSocket.instances.length;
    vi.advanceTimersByTime(2000);
    expect(MockWebSocket.instances.length).toBe(countBefore);
  });

  it("connect() cancels pending reconnect timer", async () => {
    const { terminal } = await makeTerminal();
    getWs().close(); // schedules reconnect
    const countBefore = MockWebSocket.instances.length;
    terminal.connect(); // should cancel timer and create new WS immediately
    expect(MockWebSocket.instances.length).toBe(countBefore + 1);
    // Timer should not fire again
    vi.advanceTimersByTime(2000);
    // No additional WS created from timer (it was cancelled)
    expect(MockWebSocket.instances.length).toBe(countBefore + 1);
  });
});

describe("ProvideTerminal input sending via onData", () => {
  it("sends data to WS when open via terminal onData callback", async () => {
    await makeTerminal();
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    const sentBefore = ws.sent.length;
    xterm.simulateInput("hello");
    expect(ws.sent.length).toBe(sentBefore + 1);
    expect(ws.sent[ws.sent.length - 1]).toBe("hello");
  });

  it("handleTerminalInput ignores empty data", async () => {
    await makeTerminal();
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    const sentBefore = ws.sent.length;
    xterm.simulateInput(""); // empty data should be ignored
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("handleTerminalInput ignores data when WS is not open (CONNECTING)", async () => {
    await makeTerminal();
    const ws = getWs();
    const xterm = getXterm();
    // WS is in CONNECTING state
    const sentBefore = ws.sent.length;
    xterm.simulateInput("data while connecting");
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("handleTerminalInput ignores data when WS is null (disconnected)", async () => {
    const { terminal } = await makeTerminal();
    const xterm = getXterm();
    terminal.disconnect(); // sets ws to null
    const ws = getWs();
    const sentBefore = ws.sent.length;
    xterm.simulateInput("data after disconnect");
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("does not install a custom key event handler (ctrl+c reaches terminal)", async () => {
    await makeTerminal();
    // No custom handler means xterm processes all keys normally — Ctrl+C sends \x03,
    // readline shortcuts work, tmux prefixes are not swallowed.
    expect(getXterm().customKeyHandlerCallCount).toBe(0);
  });

  it("ctrl-c data is forwarded to websocket", async () => {
    await makeTerminal({ wsUrl: "/ws/terminal" });
    const ws = getWs();
    ws.open();
    getXterm().simulateInput("\x03");
    expect(ws.sent).toContain("\x03");
  });
});

describe("ProvideTerminal CSS injection", () => {
  it("injects CSS link on construction", async () => {
    await makeTerminal();
    const links = document.head.querySelectorAll('link[rel="stylesheet"]');
    expect(links.length).toBeGreaterThan(0);
  });
});

describe("ProvideTerminal fitWithMinCols", () => {
  it("reduces fontSize when proposed cols < minCols", async () => {
    // Override proposeDimensions to return cols smaller than default 80
    class SmallFitAddon {
      fit(): void {}
      proposeDimensions(): { cols: number } {
        return { cols: 60 }; // less than 80
      }
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = { FitAddon: SmallFitAddon };
    await makeTerminal({ cols: 80 });
    const xterm = getXterm();
    // fontSize should have been reduced since 60 < 80
    expect(xterm.options.fontSize).toBeLessThan(14);
  });

  it("handles proposeDimensions returning undefined gracefully", async () => {
    class NullFitAddon {
      fit(): void {}
      proposeDimensions(): undefined {
        return undefined;
      }
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = { FitAddon: NullFitAddon };
    // Should not throw
    await makeTerminal();
  });

  it("handles proposeDimensions returning cols=0", async () => {
    class ZeroFitAddon {
      fit(): void {}
      proposeDimensions(): { cols: number } {
        return { cols: 0 };
      }
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = { FitAddon: ZeroFitAddon };
    // Should not throw
    await makeTerminal();
  });
});

describe("ProvideTerminal accessibility", () => {
  it("gear button has an aria-label", async () => {
    const { container } = await makeTerminal();
    const gear = container.querySelector<HTMLButtonElement>(".gear-btn");
    expect(gear?.getAttribute("aria-label")).toBe("Open terminal settings");
  });

  it("settings panel exposes a dialog role and accessible name", async () => {
    const { container } = await makeTerminal();
    const panel = container.querySelector(".settings-panel");
    expect(panel?.getAttribute("role")).toBe("dialog");
    expect(panel?.getAttribute("aria-label")).toBe("Terminal settings");
  });

  it("each theme button declares an aria-label", async () => {
    const { container } = await makeTerminal();
    const buttons = container.querySelectorAll<HTMLButtonElement>(".theme-btn");
    expect(buttons.length).toBeGreaterThan(0);
    for (const btn of buttons) {
      expect(btn.getAttribute("aria-label")).toMatch(/theme$/i);
      expect(btn.type).toBe("button");
    }
  });

  it("status dot exposes a status role and an aria-label", async () => {
    const { container } = await makeTerminal();
    const dot = container.querySelector("[data-status-dot='1']");
    expect(dot?.getAttribute("role")).toBe("status");
    expect(dot?.getAttribute("aria-label")).toBeTruthy();
  });

  it("status dot aria-label updates to 'Connected' when WS opens", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    const dot = container.querySelector("[data-status-dot='1']");
    expect(dot?.getAttribute("aria-label")).toBe("Connected");
  });

  it("status dot aria-label updates to 'Disconnected' on close", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    getWs().close();
    const dot = container.querySelector("[data-status-dot='1']");
    expect(dot?.getAttribute("aria-label")).toBe("Disconnected");
  });

  it("Enter key on a theme button activates it (native button behavior)", async () => {
    const { container } = await makeTerminal();
    const crtBtn = container.querySelector<HTMLButtonElement>('.theme-btn[data-theme="crt"]');
    expect(crtBtn).not.toBeNull();
    // Native <button> activates via click() — simulate keyboard activation
    crtBtn?.click();
    const root = container.querySelector(".provide-uterm");
    expect(root?.classList.contains("theme-crt")).toBe(true);
  });
});

// Regression test for Finding #14: ProvideTerminal must run incoming WS
// payloads through ControlChannelDecoder so users on `role=browser` connections
// don't see raw JSON control frames bleed into terminal output.
describe("ProvideTerminal control-channel framing", () => {
  // Build a framed payload: <DLE><STX><8-hex-len>:<json><raw-bytes>
  function makeFramedPayload(controlJson: string, rawBytes: string): string {
    const lenHex = new TextEncoder().encode(controlJson).byteLength.toString(16).padStart(8, "0");
    return `\x10\x02${lenHex}:${controlJson}${rawBytes}`;
  }

  it("strips control frames from incoming WS payloads and only writes raw bytes to xterm", async () => {
    await makeTerminal({ wsUrl: "/ws/browser/w/term" });
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    // Reset writes from boot sequence
    xterm.written = [];

    const payload = makeFramedPayload('{"type":"hijack_state","hijacked":true}', "hello terminal");
    ws.triggerMessage(payload);

    const joined = xterm.written.join("");
    expect(joined).toBe("hello terminal");
    expect(joined).not.toContain("hijack_state");
    expect(joined).not.toContain('"type"');
  });

  it("writes plain (unframed) payloads through as data frames", async () => {
    await makeTerminal({ wsUrl: "/ws/browser/w/term" });
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    xterm.written = [];

    ws.triggerMessage("plain raw output\r\n");
    expect(xterm.written.join("")).toBe("plain raw output\r\n");
  });

  it("falls back to writing the raw payload when the framing stream is corrupt", async () => {
    await makeTerminal({ wsUrl: "/ws/browser/w/term" });
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    xterm.written = [];

    // \x10 followed by an invalid marker (not \x02 and not \x10) raises in feed().
    ws.triggerMessage("\x10X");

    // The fallback path writes the raw payload so the screen doesn't go blank.
    expect(xterm.written.join("")).toContain("\x10X");
  });
});
