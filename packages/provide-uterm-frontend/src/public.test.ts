//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import {
  TerminalElement,
  UtermSessionElement,
  registerUtermElements,
} from "provide-uterm-frontend";

describe("consumer widget API", () => {
  it("exports terminal, session/hijack, and DeckMux surfaces", () => {
    expect(TerminalElement).toBeTypeOf("function");
    expect(UtermSessionElement).toBeTypeOf("function");
    expect(registerUtermElements).toBeTypeOf("function");
  });

  it("registers missing elements idempotently and preserves existing definitions", () => {
    const existingTerminal = class extends HTMLElement {};
    const definitions = new Map<string, CustomElementConstructor>([
      ["uterm-terminal", existingTerminal],
    ]);
    const registry = {
      get: (name: string) => definitions.get(name),
      define: (name: string, elementClass: CustomElementConstructor) => {
        if (definitions.has(name)) throw new Error(`duplicate definition: ${name}`);
        definitions.set(name, elementClass);
      },
    } as CustomElementRegistry;

    registerUtermElements(registry);
    registerUtermElements(registry);

    expect(definitions.get("uterm-terminal")).toBe(existingTerminal);
    expect(definitions.get("uterm-session")).toBe(UtermSessionElement);
    expect(definitions.has("uterm-approval-prompt")).toBe(true);
    expect(definitions.size).toBe(3);
  });
});
