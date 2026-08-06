//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import {
  App,
  HijackHost,
  ReplayPage,
  SessionPage,
  TerminalHost,
  createUtermExtensionRegistry,
} from "provide-uterm-app";

describe("consumer React API", () => {
  it("exports the app shell and operational surfaces", () => {
    expect(App).toBeTypeOf("function");
    expect(SessionPage).toBeTypeOf("function");
    expect(TerminalHost).toBeTypeOf("function");
    expect(HijackHost).toBeTypeOf("function");
    expect(ReplayPage).toBeTypeOf("function");
    expect(createUtermExtensionRegistry).toBeTypeOf("function");
  });
});
