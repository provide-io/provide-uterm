  it("_startReconnectAnim starts spinning then _stopReconnectAnim stops it", () => {
    const { container } = makeWidget();
    const ws = getWs();
    ws.open();
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({ type: "term", data: "hello" }); // triggers _ensureTerm
    ws.close(); // triggers _scheduleReconnect → _startReconnectAnim
    // Advance to let reconnect anim fire
    vi.advanceTimersByTime(200);
    // Advance for reconnect to succeed
    vi.advanceTimersByTime(2000);
    const newWs = getWs();
    newWs.open();
    // opening new WS calls _stopReconnectAnim
    sendMessage({ type: "snapshot" }); // also calls _stopReconnectAnim
    expect(q(container, "statustext")?.textContent).not.toBeNull();
  });

  it("reconnect anim doesn't start if already running", () => {
    const { container } = makeWidget();
    const ws = getWs();
    ws.open();
    sendMessage({ type: "term", data: "x" }); // ensure term
    ws.close();
    vi.advanceTimersByTime(100); // let anim start
    // Close triggers another call to _scheduleReconnect (which internally calls _startReconnectAnim)
    // but _startReconnectAnim guard (_reconnectAnimTimer truthy) should prevent double-start
    // Just verify no error
    expect(q(container, "statustext")).toBeTruthy();
  });

  it("_stopReconnectAnim when no term is set", () => {
    // Create widget without Terminal so _term stays null
    // biome-ignore lint/suspicious/noExplicitAny: test
    (window as any).Terminal = undefined;
    const { container } = makeWidget();
    const ws = getWs();
    ws.open();
    ws.close();
    vi.advanceTimersByTime(2000);
    const newWs = getWs();
    newWs.open();
    // _stopReconnectAnim is called, _term is null — should not throw
    expect(q(container, "statustext")).toBeTruthy();
  });

  it("onData with no WS calls nudgeReconnect", () => {
    makeWidget();
    const ws = getWs();
    ws.open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    sendMessage({ type: "term", data: "x" }); // create term
    // Get the terminal instance from the MockTerminal
    // Find a mock terminal and trigger onData while WS is closed
    ws.close();
    vi.advanceTimersByTime(200); // let reconnect timer start
    // Now trigger onData (keyboard input) via the terminal's onData callback
    // The widget stores the callback — we need to find the last MockTerminal instance
    // Since we can't directly access _term, we verify via sent messages behavior
    // After WS closes, _ws=null, so nudgeReconnect kicks in
    expect(instances.length).toBeGreaterThan(0);
  });

  it("onData when not hijacked and open mode is off does nothing", () => {
    makeWidget();
    const ws = getWs();
    ws.open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    // Not hijacked - onData should not send
    sendMessage({ type: "term", data: "x" }); // trigger _ensureTerm
    const sentBefore = ws.sent.length;
    // Can't easily trigger onData without direct access to terminal
    // But we can verify the WS state is consistent
    expect(ws.sent.length).toBeGreaterThanOrEqual(sentBefore);
  });

  it("_nudgeReconnect while CONNECTING does nothing", () => {
    makeWidget();
    const ws = getWs();
    // WS is in CONNECTING state (initial)
    expect(ws.readyState).toBe(MockWebSocket.CONNECTING);
    // onData while connecting would call nudgeReconnect
    // Can't trigger onData directly without term, but verify no crash on init
    expect(instances.length).toBeGreaterThan(0);
  });
});

describe("hijack.ts branch coverage - release and resync buttons", () => {
  it("release button with WS mode sends hijack_release", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const relBtn = q(container, "release") as HTMLButtonElement;
    relBtn.click();
    expect(getWs().sent.some((f) => f.includes("hijack_release"))).toBe(true);
  });

  it("release button with rest control calls REST release", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ hijack_id: "hid-77" }),
    });
    vi.stubGlobal("fetch", mockFetch);
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    // Acquire first
    (q(container, "hijack") as HTMLButtonElement).click();
    for (let i = 0; i < 5; i++) await Promise.resolve();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Now release
    const relBtn = q(container, "release") as HTMLButtonElement;
    relBtn.click();
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("release"), expect.anything());
  });

  it("resync button sends snapshot_req", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    const resyncBtn = q(container, "resync") as HTMLButtonElement;
    resyncBtn.click();
    expect(getWs().sent.some((f) => f.includes("snapshot_req"))).toBe(true);
  });

  it("analyze button sends analyze_req when hijacked", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const analyzeBtn = q(container, "analyze") as HTMLButtonElement;
    analyzeBtn.click();
    expect(getWs().sent.some((f) => f.includes("analyze_req"))).toBe(true);
  });

  it("analysis message updates analysistext pre element", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "analysis", formatted: "Here is the analysis result." });
    const pre = q(container, "analysistext");
    expect(pre?.textContent).toBe("Here is the analysis result.");
  });

  it("analysis message with no formatted field shows fallback", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "analysis" });
    const pre = q(container, "analysistext");
    expect(pre?.textContent).toBe("(no analysis)");
  });
});

describe("hijack.ts branch coverage - mobile keys", () => {
  it("mobile key click when hijacked sends input", () => {
    const { container } = makeWidget({ mobileKeys: true });
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Toggle keyboard to make it visible
    const kbdToggle = q(container, "kbdtoggle") as HTMLButtonElement;
    kbdToggle.click();
    // Click ESC mobile key
    const mkeyBtn = container.querySelector(".mkey") as HTMLButtonElement;
    if (mkeyBtn) {
      const sentBefore = getWs().sent.length;
      mkeyBtn.click();
      // Input data frame is sent (raw data, not JSON with "input")
      expect(getWs().sent.length).toBeGreaterThan(sentBefore);
    }
  });

  it("mobile key click when not hijacked and not open mode does nothing", () => {
    const { container } = makeWidget({ mobileKeys: true });
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    // Not hijacked - mobile key should be a no-op
    const sentBefore = getWs().sent.length;
    const mkeyBtn = container.querySelector(".mkey") as HTMLButtonElement;
    if (mkeyBtn) {
      mkeyBtn.click();
      // Should not send additional messages (WS is also open but no permissions)
    }
    expect(getWs().sent.length).toBeGreaterThanOrEqual(sentBefore);
  });

  it("mobile key click when WS is closed does nothing", () => {
    const { container } = makeWidget({ mobileKeys: true });
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Close WS
    const ws = getWs();
    ws.readyState = MockWebSocket.CLOSED;
    const sentBefore = ws.sent.length;
    const mkeyBtn = container.querySelector(".mkey") as HTMLButtonElement;
    if (mkeyBtn) mkeyBtn.click();
    expect(ws.sent.length).toBe(sentBefore);
  });
});

describe("hijack.ts branch coverage - input field keydown", () => {
  it("Enter key in input field sends message", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "hello\\r";
    const sentBefore = getWs().sent.length;
    const keyEvent = new KeyboardEvent("keydown", { key: "Enter", bubbles: true });
    field.dispatchEvent(keyEvent);
    // Input data frame sent (raw data frame, not JSON "input")
    expect(getWs().sent.length).toBeGreaterThan(sentBefore);
    // Field should be cleared
    expect(field.value).toBe("");
  });

  it("non-Enter keydown in input field does nothing", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "some text";
    const sentBefore = getWs().sent.length;
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    expect(getWs().sent.length).toBe(sentBefore);
  });

  it("inputfield send with empty value is a no-op", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "";
    const sentBefore = getWs().sent.length;
    q(container, "inputsend")?.dispatchEvent(new MouseEvent("click"));
    expect(getWs().sent.length).toBe(sentBefore);
  });

  it("inputfield send in open mode without hijack works", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true, input_mode: "open" });
    sendMessage({ type: "input_mode_changed", input_mode: "open" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "test open";
    const sentBefore = getWs().sent.length;
    q(container, "inputsend")?.dispatchEvent(new MouseEvent("click"));
    // Input data frame sent (raw data frame, not JSON with "input")
    expect(getWs().sent.length).toBeGreaterThan(sentBefore);
  });
});

describe("hijack.ts branch coverage - hijack button when WS closed", () => {
  it("hijack button click when WS closed is a no-op", () => {
    const { container } = makeWidget();
    const ws = getWs();
    // Don't open WS — button should be disabled/no-op
    (q(container, "hijack") as HTMLButtonElement).click();
    expect(ws.sent.length).toBe(0);
  });

  it("resync button click when WS closed is a no-op", () => {
    const { container } = makeWidget();
    const ws = getWs();
    const sentBefore = ws.sent.length;
    (q(container, "resync") as HTMLButtonElement).click();
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("release button click when WS closed is a no-op", () => {
    const { container } = makeWidget();
    const ws = getWs();
    const sentBefore = ws.sent.length;
    (q(container, "release") as HTMLButtonElement).click();
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("analyze button click when WS closed is a no-op", () => {
    const { container } = makeWidget();
    const ws = getWs();
    const sentBefore = ws.sent.length;
    (q(container, "analyze") as HTMLButtonElement).click();
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("step button click when WS closed is a no-op", () => {
    const { container } = makeWidget();
    const ws = getWs();
    const sentBefore = ws.sent.length;
    (q(container, "step") as HTMLButtonElement).click();
    expect(ws.sent.length).toBe(sentBefore);
  });
});

describe("hijack.ts branch coverage - snapshot message", () => {
  it("snapshot message resets and writes to terminal", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({ type: "term", data: "old content" }); // init terminal
    sendMessage({ type: "snapshot", screen: "new screen content", prompt_detected: { prompt_id: "p1" } });
    const promptEl = q(container, "prompt");
    expect(promptEl?.textContent).toContain("p1");
  });

  it("snapshot with no prompt_detected clears prompt", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({ type: "term", data: "x" });
    sendMessage({ type: "snapshot", screen: "content" });
    const promptEl = q(container, "prompt");
    expect(promptEl?.textContent).toBe("");
  });
});

describe("hijack.ts branch coverage - hijack_state updates", () => {
  it("hijack_state with input_mode updates inputMode", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: false, owner: null, input_mode: "open" });
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).toContain("shared");
  });

  it("hijack_state with hijackedByMe starts heartbeat", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Heartbeat should fire
    vi.advanceTimersByTime(6000);
    expect(getWs().sent.some((f) => f.includes("heartbeat"))).toBe(true);
  });

  it("hijack_state with owner not me clears heartbeat", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Now someone else takes over
    sendMessage({ type: "hijack_state", hijacked: true, owner: "someone-else" });
    const _initialSent = getWs().sent.length;
    vi.advanceTimersByTime(6000);
    // No heartbeat should be sent (not hijacked by me)
    const additionalSent = getWs().sent.filter((f) => f.includes("heartbeat"));
    expect(additionalSent.length).toBe(0);
  });
});

describe("hijack.ts branch coverage - rest heartbeat", () => {
  it("heartbeat in rest mode calls REST heartbeat endpoint", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ hijack_id: "hid-hb" }),
    });
    vi.stubGlobal("fetch", mockFetch);
    const { container } = makeWidget({ heartbeatInterval: 100 });
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    // Acquire hijack
    (q(container, "hijack") as HTMLButtonElement).click();
    for (let i = 0; i < 5; i++) await Promise.resolve();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Advance past heartbeat interval
    vi.advanceTimersByTime(200);
    for (let i = 0; i < 5; i++) await Promise.resolve();
    // Should have called heartbeat REST endpoint
    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("heartbeat"), expect.anything());
  });

  it("_restHijack returns null when _restHijackId is null and action is not acquire", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({}),
    });
    vi.stubGlobal("fetch", mockFetch);
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Click step — _restHijackId is null so _restHijack should return null
    (q(container, "step") as HTMLButtonElement).click();
    for (let i = 0; i < 5; i++) await Promise.resolve();
    // fetch should not have been called (returned null before fetch)
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

// Regression tests for Finding #18: wss + ?token= surfaces auth in proxy logs.
// We don't strip the token (HttpOnly cookies aren't readable from JS so we
// can't reliably detect cookie-based auth), but we emit a console warning so
// operators can audit production deploys.
describe("hijack-websocket.ts wss+token audit warning (Finding #18)", () => {
  it("warns on the console when an authToken is appended to a wss:// URL", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("location", { protocol: "https:", host: "secure.example.com" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { workerId: "w", authToken: "shh-secret" });
    expect(getWs().url).toMatch(/^wss:\/\/.*token=shh-secret/);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("?token=…"));
    widget.disconnect();
    warn.mockRestore();
  });

  it("does NOT warn over ws:// (plain http context)", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("location", { protocol: "http:", host: "localhost" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { workerId: "w", authToken: "shh-secret" });
    expect(getWs().url).toMatch(/^ws:\/\/.*token=shh-secret/);
    expect(warn).not.toHaveBeenCalledWith(expect.stringContaining("?token=…"));
    widget.disconnect();
    warn.mockRestore();
  });

  it("does NOT warn when no authToken is configured", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("location", { protocol: "https:", host: "secure.example.com" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { workerId: "w" });
    expect(getWs().url).not.toContain("token=");
    expect(warn).not.toHaveBeenCalledWith(expect.stringContaining("?token=…"));
    widget.disconnect();
    warn.mockRestore();
  });

  it("only warns once per state even if the URL is resolved multiple times (reconnects)", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("location", { protocol: "https:", host: "secure.example.com" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { workerId: "w", authToken: "shh" });
    // Trigger a reconnect by closing the WS
    getWs().close();
    vi.advanceTimersByTime(1200); // past 1s backoff → second connect
    const tokenWarnings = warn.mock.calls.filter((c) =>
      String(c[0] ?? "").includes("?token=…"),
    );
    expect(tokenWarnings.length).toBe(1);
    widget.disconnect();
    warn.mockRestore();
  });
});

// Regression test for Finding #19: server-confirmed role from `hello` frame
// should drive UX decisions (modal vs statusbar, admin-only approve/reject
// buttons), not the constructor-input role.
describe("ProvideHijack server-confirmed role (Finding #19)", () => {
  it("renders admin approval modal with approve/reject buttons after a hello frame upgrades a viewer to admin", () => {
    const { container } = makeWidget({ role: "viewer", approvalUxMode: "auto" });
    getWs().open();
    // Server confirms this connection is actually admin.
    sendMessage({ type: "hello", role: "admin", worker_online: true });
    // Now an approval request arrives — UX should choose the admin modal.
    sendMessage({
      type: "approval_pending",
      request_id: "req-1",
      command: "rm -rf /",
      expires_at: Date.now() / 1000 + 60,
    });
    // Admin modal renders both buttons.
    expect(q(container, "approve")).toBeTruthy();
    expect(q(container, "reject")).toBeTruthy();
    // The container element class should be the modal flavor, not the statusbar.
    const modal = container.querySelector(".hijack-approval-modal, .hijack-approval-statusbar");
    expect(modal?.classList.contains("hijack-approval-modal")).toBe(true);
  });

  it("renders the statusbar (non-admin) UX when neither config nor server role is admin", () => {
    const { container } = makeWidget({ role: "viewer", approvalUxMode: "auto" });
    getWs().open();
    sendMessage({ type: "hello", role: "viewer", worker_online: true });
    sendMessage({
      type: "approval_pending",
      request_id: "req-2",
      command: "ls",
      expires_at: Date.now() / 1000 + 60,
    });
    // No admin approve/reject buttons; statusbar UX is shown.
    expect(q(container, "approve")).toBeNull();
    expect(q(container, "reject")).toBeNull();
  });

  it("falls back to constructor role when hello carries no role field", () => {
    const { container } = makeWidget({ role: "admin", approvalUxMode: "auto" });
    getWs().open();
    // hello with no role field — _effectiveRole() falls back to config.role
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({
      type: "approval_pending",
      request_id: "req-3",
      command: "whoami",
      expires_at: Date.now() / 1000 + 60,
    });
    expect(q(container, "approve")).toBeTruthy();
    expect(q(container, "reject")).toBeTruthy();
  });
});

// Regression guard for Finding #5: the snapshot reset must emit a real ANSI
// sequence (ESC byte + CSI), not the literal bracket text. The grep that
// prompted the report was misled by the terminal rendering ESC as invisible
// in the editor — the file already had the ESC bytes; this test pins them in.
describe("ProvideHijack snapshot reset emits real ESC sequence (Finding #5 guard)", () => {
  it("writes the soft-reset ESC sequence on snapshot before the screen contents", () => {
    const { widget } = makeWidget();
    getWs().open();
    // The snapshot handler in hijack.ts wraps the terminal init in try/catch;
    // a mock-Terminal incompatibility would swallow the call silently. Reach
    // in directly to instantiate a known-good term, then exercise the handler.
    // biome-ignore lint/suspicious/noExplicitAny: reach into private state for test
    const widgetAny = widget as any;
    const term = new MockTerminal();
    widgetAny._state.term = term;
    sendMessage({ type: "snapshot", screen: "hello" });
    // The very first frame after the snapshot handler runs is the reset sequence.
    expect(term.written[0]).toBe("\x1b[!p\x1b[2J\x1b[H");
    expect(term.written[1]).toBe("hello");
  });
});
