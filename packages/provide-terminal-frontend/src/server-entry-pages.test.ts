//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Tests for server entry page modules that just call bootApp().
// These files contain a single top-level `void bootApp()` statement.
import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the boot module to prevent actual bootApp() execution
vi.mock("./app/boot.js", () => ({
  bootApp: vi.fn().mockResolvedValue(undefined),
}));

import * as bootModule from "./app/boot.js";

describe("server-replay-page", () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
  });

  it("calls bootApp on module load", async () => {
    await import("./server-replay-page.js");
    // bootApp should have been called
    expect(bootModule.bootApp).toHaveBeenCalled();
  });
});

describe("server-session-page", () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
  });

  it("calls bootApp on module load", async () => {
    await import("./server-session-page.js");
    expect(bootModule.bootApp).toHaveBeenCalled();
  });
});
