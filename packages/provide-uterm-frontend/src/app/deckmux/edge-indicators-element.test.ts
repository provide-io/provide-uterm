//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import "./edge-indicators-element.js";
import type { EdgeIndicatorsElement, EdgeIndicatorUser } from "./edge-indicators-element.js";

describe("uterm-edge-indicators", () => {
  let element: EdgeIndicatorsElement;

  beforeEach(() => {
    element = document.createElement("uterm-edge-indicators") as EdgeIndicatorsElement;
    document.body.appendChild(element);
  });

  afterEach(() => {
    element.remove();
  });

  it("renders a track", async () => {
    await element.updateComplete;
    const track = element.shadowRoot?.querySelector(".dm-edge-track");
    expect(track).not.toBeNull();
  });

  it("renders users", async () => {
    const users: EdgeIndicatorUser[] = [
      {
        userId: "user-1",
        slot: 0,
        color: "#ff0000",
        range: { top: 0, height: 0.5 },
        options: { name: "Alice", isOwner: true }
      }
    ];

    element.users = users;
    await element.updateComplete;

    const bars = element.shadowRoot?.querySelectorAll(".dm-edge-bar");
    expect(bars?.length).toBe(1);

    const bar = bars![0] as HTMLElement;
    expect(bar.dataset.userId).toBe("user-1");
    expect(bar.classList.contains("dm-edge-bar--owner")).toBe(true);

    const name = bar.querySelector(".dm-edge-name");
    expect(name).not.toBeNull();
    expect(name?.textContent).toBe("Alice");
  });
});
