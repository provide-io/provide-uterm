import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./context-menu.js";
import type { ContextMenu } from "./context-menu.js";
import type { DeckMuxUser } from "./types.js";

describe("ContextMenu", () => {
  let el: ContextMenu;

  beforeEach(() => {
    el = document.createElement("uterm-context-menu") as ContextMenu;
    document.body.appendChild(el);
  });

  afterEach(() => {
    el.remove();
  });

  const mockUser: DeckMuxUser = {
    userId: "u1",
    name: "Alice",
    color: "#ff0000",
    initials: "AL",
    role: "admin",
    typing: false,
    cols: 80,
    rows: 24,
  };

  it("renders nothing if user is not provided", async () => {
    await el.updateComplete;
    const header = el.shadowRoot!.querySelector(".dm-context-menu-header");
    expect(header).toBeNull();
  });

  it("renders user name and color dot", async () => {
    el.user = mockUser;
    await el.updateComplete;

    const nameEl = el.shadowRoot!.querySelector(".dm-context-menu-header span:last-child");
    expect(nameEl!.textContent).toBe("Alice");

    const dotEl = el.shadowRoot!.querySelector(".dm-context-menu-dot") as HTMLElement;
    // JSDOM rgb conversion
    expect(dotEl.style.background).toMatch(/rgb\(255, 0, 0\)|#ff0000|red/);
  });

  it("renders actions and handles clicks", async () => {
    const onClick1 = vi.fn();
    const onClick2 = vi.fn();

    el.user = mockUser;
    el.actions = [
      { id: "1", label: "Action 1", icon: "🚀", onClick: onClick1 },
      { id: "2", label: "Action 2", sublabel: "Sub 2", icon: "🔥", danger: true, onClick: onClick2 },
    ];
    await el.updateComplete;

    const buttons = el.shadowRoot!.querySelectorAll("button");
    expect(buttons.length).toBe(2);

    // Check first action
    expect(buttons[0].textContent).toContain("Action 1");
    expect(buttons[0].textContent).toContain("🚀");
    expect(buttons[0].classList.contains("dm-context-menu-item--danger")).toBe(false);
    buttons[0].click();
    expect(onClick1).toHaveBeenCalled();

    // Check second action
    expect(buttons[1].textContent).toContain("Action 2");
    expect(buttons[1].textContent).toContain("Sub 2");
    expect(buttons[1].textContent).toContain("🔥");
    expect(buttons[1].classList.contains("dm-context-menu-item--danger")).toBe(true);
    buttons[1].click();
    expect(onClick2).toHaveBeenCalled();
  });
});
