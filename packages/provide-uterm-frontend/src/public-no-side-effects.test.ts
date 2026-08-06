//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { expect, it } from "vitest";
import { registerUtermElements } from "provide-uterm-frontend";

it("does not register custom elements until the consumer asks", () => {
  expect(customElements.get("uterm-terminal")).toBeUndefined();
  expect(customElements.get("uterm-session")).toBeUndefined();
  expect(customElements.get("uterm-approval-prompt")).toBeUndefined();
  registerUtermElements();
  expect(customElements.get("uterm-terminal")).toBeTypeOf("function");
  expect(customElements.get("uterm-session")).toBeTypeOf("function");
  expect(customElements.get("uterm-approval-prompt")).toBeTypeOf("function");
});
