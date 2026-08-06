//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { UtermSessionElement, registerUtermSessionElement } from "./session-element";
import { TerminalElement, registerTerminalElement } from "./terminal-element";

export { TerminalElement, UtermSessionElement };
export type * from "./app/deckmux/types";

export function registerUtermElements(registry: CustomElementRegistry = customElements): void {
  registerTerminalElement(registry);
  registerUtermSessionElement(registry);
}
