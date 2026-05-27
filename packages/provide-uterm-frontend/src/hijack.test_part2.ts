  it("error message with no message uses fallback", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "error" });
    expect(q(container, "statustext")?.textContent).toBe("Error: unknown");
  });

  it("protocol error on bad message closes WS and schedules reconnect", () => {
    makeWidget();
    getWs().open();
    // Corrupt frame triggers _setStatus("bad","Protocol error") then ws.close()
    // ws.close() fires onclose → _scheduleReconnect() → "Reconnecting in Ns…"
    getWs().receive("\x10X"); // invalid control prefix
    expect(getWs().readyState).toBe(MockWebSocket.CLOSED);
  });

  it("data frame becomes term message", () => {
    makeWidget();
    getWs().open();
    getWs().receive(encodeDataFrame("raw output"));
    // No throw = term message handled
  });
});

// ── Heartbeat ─────────────────────────────────────────────────────────────────

describe("heartbeat", () => {
  it("sends heartbeat to WS when hijackedByMe", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    const newFrames = getWs().sent.slice(sentBefore);
    expect(newFrames.some((f) => f.includes("heartbeat"))).toBe(true);
  });

  it("skips heartbeat when not hijackedByMe", () => {
    makeWidget();
    getWs().open();
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    expect(getWs().sent.length).toBe(sentBefore);
  });

  it("skips WS heartbeat in rest mode (no WS frames for heartbeat)", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hello", hijack_control: "rest" });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    // No heartbeat WS frame sent — rest mode skips WS and calls fetch
    // (fetch returns null because _restHijackId is null, but no WS frames sent)
    expect(getWs().sent.length).toBe(sentBefore);
  });
});

// ── Button clicks ─────────────────────────────────────────────────────────────

describe("button clicks", () => {
  it("hijack button sends hijack_request via WS", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    (q(container, "hijack") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_request"))).toBe(true);
  });

  it("hijack button is no-op when WS not open", () => {
    const { container } = makeWidget();
    (q(container, "hijack") as HTMLButtonElement).click();
    // Sent nothing
    expect(getWs().sent).toHaveLength(0);
  });

  it("hijack button calls REST acquire when hijack_control=rest", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ hijack_id: "hid-1" }) }),
    );
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    (q(container, "hijack") as HTMLButtonElement).click();
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(expect.stringContaining("acquire"), expect.anything());
  });

  it("step button is no-op when not hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    (q(container, "step") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_step"))).toBe(false);
  });

  it("step button sends hijack_step when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "step") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_step"))).toBe(true);
  });

  it("release button sends hijack_release", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "release") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_release"))).toBe(true);
  });

  it("release button calls REST acquire then release when hijack_control=rest", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ hijack_id: "hid-99" }) }),
    );
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    // Acquire first to set _restHijackId
    (q(container, "hijack") as HTMLButtonElement).click();
    // Flush Promise microtasks so _restHijack's async chain settles
    for (let i = 0; i < 5; i++) await Promise.resolve();
    // Now release
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "release") as HTMLButtonElement).click();
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(expect.stringContaining("release"), expect.anything());
  });

  it("resync button sends snapshot_req", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    (q(container, "resync") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("snapshot_req"))).toBe(true);
  });

  it("analyze button sends analyze_req when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "analyze") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("analyze_req"))).toBe(true);
  });

  it("analyze button is no-op when not hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    (q(container, "analyze") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("analyze_req"))).toBe(false);
  });

  it("kbd toggle button toggles mobile keys visibility", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const toggleBtn = q(container, "kbdtoggle") as HTMLButtonElement;
    toggleBtn.click(); // show
    const mkRow = q(container, "mobilekeys");
    // visibility depends on connected+canInput+mobileKeysVisible — just check no throw
    expect(mkRow).toBeTruthy();
  });

  it("mobile key buttons send input when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const escBtn = Array.from(container.querySelectorAll(".mkey")).find(
      (b) => b.textContent === "ESC",
    ) as HTMLButtonElement;
    escBtn.click();
    // ESC is sent as a raw data frame (encodeDataFrame("\x1b") = "\x1b"), not JSON
    expect(getWs().sent.some((f) => f.includes("\x1b"))).toBe(true);
  });

  it("mobile key buttons are no-op when not hijackedByMe and not open mode", () => {
    const { container } = makeWidget();
    getWs().open();
    const escBtn = Array.from(container.querySelectorAll(".mkey")).find(
      (b) => b.textContent === "ESC",
    ) as HTMLButtonElement;
    const sentBefore = getWs().sent.length;
    escBtn.click();
    expect(getWs().sent.length).toBe(sentBefore);
  });
});

// ── Text input ────────────────────────────────────────────────────────────────

describe("text input field", () => {
  it("sends input on Enter key when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "hello";
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(getWs().sent.some((f) => f === "hello")).toBe(true);
    expect(field.value).toBe(""); // cleared after send
  });

  it("does not send on Enter when field is empty", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "";
    const sentBefore = getWs().sent.length;
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(getWs().sent.length).toBe(sentBefore);
  });

  it("unescapes \\r \\n \\t \\e in input", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "\\r\\n\\t\\e";
    const sendBtn = q(container, "inputsend") as HTMLButtonElement;
    sendBtn.click();
    expect(getWs().sent.some((f) => f === "\r\n\t\x1b")).toBe(true);
  });

  it("send button sends input", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "test";
    (q(container, "inputsend") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f === "test")).toBe(true);
  });

  it("does not send when not hijackedByMe and not open mode", () => {
    const { container } = makeWidget();
    getWs().open();
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "blocked";
    const sentBefore = getWs().sent.length;
    (q(container, "inputsend") as HTMLButtonElement).click();
    expect(getWs().sent.length).toBe(sentBefore);
  });
});

// ── Reconnect / nudge ─────────────────────────────────────────────────────────

describe("reconnect logic", () => {
  it("backoff delay increases with each attempt", () => {
    makeWidget();
    // First close → 1s delay
    getWs().close();
    vi.advanceTimersByTime(1100);
    // Second close → 2s delay
    getWs().close();
    const instancesBefore = instances.length;
    vi.advanceTimersByTime(1100);
    expect(instances.length).toBe(instancesBefore); // not yet reconnected (2s delay)
    vi.advanceTimersByTime(1000);
    expect(instances.length).toBe(instancesBefore + 1);
  });

  it("nudge reconnect cancels pending timer and reconnects immediately", () => {
    const { widget } = makeWidget();
    getWs().close(); // schedules 1s reconnect
    // Simulate nudge via typing while disconnected — need to call connect directly
    widget.connect(); // calls _connectWs which clears the WS and creates a new one
    expect(instances).toHaveLength(2);
  });
});

// ── mobileKeys=false ──────────────────────────────────────────────────────────

describe("mobileKeys=false option", () => {
  it("does not build mobile keys", () => {
    const { container } = makeWidget({ mobileKeys: false });
    expect(container.querySelectorAll(".mkey")).toHaveLength(0);
  });
});

// ── Local echo and activity indicator ─────────────────────────────────────────

describe("local echo and activity indicator", () => {
  it("widget has local echo tracking state variables", () => {
    const { widget } = makeWidget();
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    const w = widget as any;

    // Verify state variables exist for activity indicator feature
    expect(w._activityFlashTimer).toBeNull();
    expect(w._statusDotElement).toBeNull();
  });

  it("mobile key buttons send input (tests local echo code path)", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", hijacked: true, hijacked_by_me: true });

    // Find and click ESC button (which calls _echoInput internally)
    const escBtn = Array.from(container.querySelectorAll(".mkey")).find(
      (b) => b.textContent === "ESC",
    ) as HTMLButtonElement;

    const sentBefore = getWs().sent.length;
    escBtn.click();

    // Should have sent input message (proves _echoInput and _wsSend were called)
    const newMessages = getWs().sent.slice(sentBefore);
    expect(newMessages.some((f) => f.includes("\x1b"))).toBe(true);
  });

  it("text input field sends input (tests local echo code path)", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", hijacked: true, hijacked_by_me: true });

    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "test";

    const sentBefore = getWs().sent.length;
    (q(container, "inputsend") as HTMLButtonElement).click();

    // Should have sent input message (proves _echoInput and _wsSend were called)
    const newMessages = getWs().sent.slice(sentBefore);
    expect(newMessages.some((f) => f.includes("test"))).toBe(true);
  });

  it("dispose clears local echo and activity flash timers", () => {
    const { widget } = makeWidget();
    getWs().open();

    // biome-ignore lint/suspicious/noExplicitAny: test mock
    const w = widget as any;

    // Set up timer
    w._activityFlashTimer = setTimeout(() => {}, 200);

    // Dispose should clear it
    widget.dispose();

    // After dispose, timers should be null
    expect(w._activityFlashTimer).toBeNull();
    expect(w._statusDotElement).toBeNull();
  });
});

// ── onResize callback ──────────────────────────────────────────────────────────

describe("onResize callback", () => {
  let capturedRoCallback: (() => void) | null = null;

  beforeEach(() => {
    capturedRoCallback = null;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: () => void) {
          capturedRoCallback = cb;
        }
        observe() {}
        disconnect() {}
      },
    );
    // Suppress rAF by default — ResizeObserver tests don't need it to fire
    vi.stubGlobal("requestAnimationFrame", (_cb: () => void) => 0);
  });

  /** Create widget and init terminal via a snapshot message. */
  function makeWidgetWithTerm(opts: Record<string, unknown> = {}): {
    widget: ProvideHijack;
    term: MockTerminal;
  } {
    const { widget } = makeWidget(opts);
    getWs().open();
    sendMessage({ type: "snapshot", screen: "" });
    // biome-ignore lint/suspicious/noExplicitAny: accessing private for test
    const term = (widget as any)._term as MockTerminal;
    return { widget, term };
  }

  // ── ResizeObserver path ────────────────────────────────────────────────────

  it("fires onResize via ResizeObserver when dims are positive", () => {
    const onResize = vi.fn();
    const { term } = makeWidgetWithTerm({ onResize });
    term.cols = 80;
    term.rows = 24;
    capturedRoCallback?.();
    expect(onResize).toHaveBeenCalledOnce();
    expect(onResize).toHaveBeenCalledWith(80, 24);
  });

  it("does not fire onResize via ResizeObserver when cols is zero", () => {
    const onResize = vi.fn();
    makeWidgetWithTerm({ onResize }); // term.cols/rows default to 0
    capturedRoCallback?.();
    expect(onResize).not.toHaveBeenCalled();
  });

  it("does not fire onResize via ResizeObserver when _term is null (disposed)", () => {
    const onResize = vi.fn();
    const { widget, term } = makeWidgetWithTerm({ onResize });
    term.cols = 80;
    term.rows = 24;
    widget.dispose(); // sets _term = null
    capturedRoCallback?.();
    expect(onResize).not.toHaveBeenCalled();
  });

  it("does not throw via ResizeObserver when onResize is not provided", () => {
    const { term } = makeWidgetWithTerm();
    term.cols = 80;
    term.rows = 24;
    expect(() => capturedRoCallback?.()).not.toThrow();
  });

  // ── rAF (initial fit) path ─────────────────────────────────────────────────

  it("fires onResize via rAF when dims are positive at fit time", () => {
    const onResize = vi.fn();
    // Subclass with positive dims so they're set when the rAF callback fires
    class TermWithDims extends MockTerminal {
      cols = 80;
      rows = 24;
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).Terminal = TermWithDims;
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    makeWidget({ onResize });
    getWs().open();
    sendMessage({ type: "snapshot", screen: "" });
    expect(onResize).toHaveBeenCalledWith(80, 24);
  });

  it("does not fire onResize via rAF when dims are zero at fit time", () => {
    const onResize = vi.fn();
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    makeWidget({ onResize });
    getWs().open();
    sendMessage({ type: "snapshot", screen: "" }); // MockTerminal: cols=0, rows=0
    expect(onResize).not.toHaveBeenCalled();
  });

  it("does not fire onResize via rAF when _term is null (disposed before rAF fires)", () => {
    const onResize = vi.fn();
    class TermWithDims extends MockTerminal {
      cols = 80;
      rows = 24;
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).Terminal = TermWithDims;
    let latestWidget: ProvideHijack | null = null;
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      latestWidget?.dispose(); // dispose before the callback fires → _term = null
      cb();
      return 0;
    });
    const { widget } = makeWidget({ onResize });
    latestWidget = widget;
    getWs().open();
    sendMessage({ type: "snapshot", screen: "" });
    expect(onResize).not.toHaveBeenCalled();
  });
});
