import { describe, it, expect, beforeEach, afterEach } from "vitest";
import "./cursor-overlay-element.js";
import type { CursorOverlayElement, } from "./cursor-overlay-element.js";

describe("CursorOverlayElement", () => {
  let el: CursorOverlayElement;

  beforeEach(() => {
    el = document.createElement("uterm-cursor-overlay") as CursorOverlayElement;
    document.body.appendChild(el);
  });

  afterEach(() => {
    el.remove();
  });

  it("should render nothing by default", () => {
    expect(el.shadowRoot?.querySelectorAll(".dm-pin").length).toBe(0);
    expect(el.shadowRoot?.querySelectorAll(".dm-selection").length).toBe(0);
  });

  it("should hide overlay when visible is false", async () => {
    el.visible = false;
    // wait for Lit to update
    await el.updateComplete;
    
    const overlay = el.shadowRoot?.querySelector(".dm-cursor-overlay") as HTMLElement;
    expect(overlay.style.display).toBe("none");
  });

  it("should render pins correctly", async () => {
    el.users = [
      {
        userId: "user1",
        name: "Alice",
        color: "#ff0000",
        pin: { line: 5 },
      },
    ];
    await el.updateComplete;

    const pins = el.shadowRoot?.querySelectorAll(".dm-pin");
    expect(pins?.length).toBe(1);

    const pin = pins![0] as HTMLElement;
    expect(pin.dataset.userId).toBe("user1");
    expect(pin.style.getPropertyValue("--dm-user-color")).toBe("#ff0000");
    expect(pin.style.top).toBe("5lh");
    
    const label = pin.querySelector(".dm-pin-label");
    expect(label?.textContent).toContain("Alice");
    expect(label?.textContent).toContain("📌");
  });

  it("should mark owner pin", async () => {
    el.ownerId = "user1";
    el.users = [
      {
        userId: "user1",
        name: "Bob",
        color: "#00ff00",
        pin: { line: 10 },
      },
    ];
    await el.updateComplete;

    const pin = el.shadowRoot?.querySelector(".dm-pin");
    expect(pin?.classList.contains("dm-pin--owner")).toBe(true);

    const label = pin?.querySelector(".dm-pin-label");
    expect(label?.textContent).toContain("⌨️");
    expect(label?.textContent).toContain("Bob");
  });

  it("should render selections correctly", async () => {
    el.users = [
      {
        userId: "user2",
        name: "Charlie",
        color: "#0000ff",
        selection: { startLine: 2, endLine: 4 },
      },
    ];
    await el.updateComplete;

    const selections = el.shadowRoot?.querySelectorAll(".dm-selection");
    expect(selections?.length).toBe(1);

    const sel = selections![0] as HTMLElement;
    expect(sel.dataset.userId).toBe("user2");
    expect(sel.style.getPropertyValue("--dm-user-color")).toBe("#0000ff");
    expect(sel.style.top).toBe("2lh");
    expect(sel.style.height).toBe("3lh"); // 4 - 2 + 1
  });
});
