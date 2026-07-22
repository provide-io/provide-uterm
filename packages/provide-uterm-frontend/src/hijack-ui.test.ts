//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import { MOBILE_KEYS } from "./hijack-ui.js";

describe("MOBILE_KEYS", () => {
  it("starts with ESC", () => {
    expect(MOBILE_KEYS[0]).toEqual({ label: "ESC", data: "\x1b" });
  });

  it("contains arrow keys", () => {
    const labels = MOBILE_KEYS.map((k) => k.label);
    expect(labels).toEqual(expect.arrayContaining(["↑", "↓", "→", "←"]));
  });
});
