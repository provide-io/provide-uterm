//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { UtermSessionElement, registerUtermSessionElement } from "./session-element";
import { TerminalElement, registerTerminalElement } from "./terminal-element";

export { TerminalElement, UtermSessionElement };
export {
  FINAL_CLOSE_CODES,
  UTERM_CLOSE_EVENT,
  UTERM_CONNECTION_EVENT,
  VIEWER_REVOKED_CLOSE_CODE,
  isFinalCloseCode,
} from "./connection-events";
export type { UtermCloseEventDetail, UtermConnectionEventDetail } from "./connection-events";
export type * from "./app/deckmux/types";

export function registerUtermElements(registry: CustomElementRegistry = customElements): void {
  registerTerminalElement(registry);
  registerUtermSessionElement(registry);
}
