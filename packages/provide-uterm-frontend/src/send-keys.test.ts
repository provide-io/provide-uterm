//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { SEND_KEYS_HELP, unescapeSendKeys } from "./send-keys.js";

describe("unescapeSendKeys", () => {
  it("turns the common escapes into the keys they name", () => {
    expect(unescapeSendKeys("\\r\\n\\t\\e")).toBe("\r\n\t\x1b");
  });

  it("reads \\x1b, and any \\xHH, as that byte", () => {
    expect(unescapeSendKeys("\\x1b[A")).toBe("\x1b[A");
    expect(unescapeSendKeys("\\x1B[B\\x03")).toBe("\x1b[B\x03");
  });

  it("reads \\\\ as one backslash, so a literal \\r can still be typed", () => {
    expect(unescapeSendKeys("a\\\\b")).toBe("a\\b");
    expect(unescapeSendKeys("\\\\r")).toBe("\\r");
    expect(unescapeSendKeys("\\\\\\r")).toBe("\\\r");
  });

  it("leaves anything else as typed", () => {
    expect(unescapeSendKeys("plain text")).toBe("plain text");
    expect(unescapeSendKeys("\\q \\x1 \\xzz trailing\\")).toBe("\\q \\x1 \\xzz trailing\\");
  });

  it("documents every escape it reads", () => {
    for (const named of ["\\r", "\\n", "\\t", "\\e", "\\x1b", "\\\\"]) {
      expect(SEND_KEYS_HELP).toContain(named);
    }
  });
});
