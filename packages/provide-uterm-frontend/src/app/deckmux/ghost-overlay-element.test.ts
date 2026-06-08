//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import "./ghost-overlay-element.js";
import type { UtermGhostOverlayElement } from "./ghost-overlay-element.js";

describe("UtermGhostOverlayElement", () => {
  let el: UtermGhostOverlayElement;

  beforeEach(() => {
    el = document.createElement("uterm-ghost-overlay") as UtermGhostOverlayElement;
    document.body.appendChild(el);
  });

  afterEach(() => {
    el.remove();
  });

  it("renders nothing when visible is false", async () => {
    el.visible = false;
    await el.updateComplete;
    expect(el.shadowRoot!.querySelectorAll(".dm-ghost-box").length).to.equal(0);
  });

  it("renders nothing when ownCols or ownRows is 0", async () => {
    el.visible = true;
    el.ownCols = 0;
    el.ownRows = 24;
    await el.updateComplete;
    expect(el.shadowRoot!.querySelectorAll(".dm-ghost-box").length).to.equal(0);
  });

  it("renders boxes with correct sizes and colors based on dimensions", async () => {
    const entries = [
      {
        userId: "u1",
        color: "#ff0000",
        cols: 40,
        rows: 12,
        hidden: false,
        flash: false,
      },
    ];
    el.visible = true;
    el.ownCols = 80;
    el.ownRows = 24;
    el.entries = entries;
    await el.updateComplete;

    const box = el.shadowRoot!.querySelector<HTMLElement>(".dm-ghost-box");
    expect(box).to.exist;
    expect(box!.dataset.userId).to.equal("u1");
    // pctW = (40/80)*100 = 50%, pctH = (12/24)*100 = 50%
    expect(box!.style.width).to.equal("50%");
    expect(box!.style.height).to.equal("50%");
    expect(box!.style.getPropertyValue("--dm-user-color")).to.equal("#ff0000");
  });

  it("applies hidden and flash classes", async () => {
    const entries = [
      {
        userId: "u1",
        color: "#ff0000",
        cols: 80,
        rows: 24,
        hidden: true,
        flash: true,
      },
    ];
    el.visible = true;
    el.ownCols = 80;
    el.ownRows = 24;
    el.entries = entries;
    await el.updateComplete;

    const box = el.shadowRoot!.querySelector(".dm-ghost-box");
    expect(box!.classList.contains("dm-ghost-box--hidden")).to.be.true;
    expect(box!.classList.contains("dm-ghost-box--flash")).to.be.true;
  });
});
