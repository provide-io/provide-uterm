//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Hyperlink policy for terminal output.
 *
 * Everything a terminal renders is untrusted: it is whatever the far end wrote,
 * and OSC 8 lets that end attach any URI it likes to any run of text. xterm.js
 * ships a default handler that navigates to that URI after a confirm, with no
 * check on the scheme — so `\x1b]8;;javascript:...\x1b\\` becomes a clickable
 * script in the page hosting the terminal, and a capture or a replay carries it
 * to whoever reads the session afterwards.
 *
 * Only schemes that can do nothing but load a document are followed. The confirm
 * is kept, because a link printed by a remote host is still a link nobody asked
 * for; what changes is that the ones it cannot vouch for are dropped rather than
 * handed to the browser.
 */

const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/** Whether this URI is one the page may follow at all. */
export function isSafeTerminalLink(uri: string): boolean {
  try {
    return SAFE_SCHEMES.has(new URL(uri).protocol);
  } catch {
    // Relative, malformed, or scheme-less: there is nothing here to vouch for.
    return false;
  }
}

/** xterm's `ILinkHandler`, narrowed to what a link handler is allowed to be. */
export interface TerminalLinkHandler {
  activate(event: MouseEvent, uri: string): void;
}

/**
 * A link handler that opens document URIs and silently ignores the rest.
 *
 * `noopener` matters as much as the scheme check: without it the opened page
 * reaches back through `window.opener` into the page holding the terminal.
 */
export function safeLinkHandler(
  confirmNavigation: (uri: string) => boolean = defaultConfirm,
): TerminalLinkHandler {
  return {
    activate(_event: MouseEvent, uri: string): void {
      if (!isSafeTerminalLink(uri)) return;
      if (!confirmNavigation(uri)) return;
      window.open(uri, "_blank", "noopener,noreferrer");
    },
  };
}

function defaultConfirm(uri: string): boolean {
  return window.confirm(`Do you want to navigate to ${uri}?`);
}

/**
 * A link handler that follows nothing.
 *
 * For views that read back a session rather than drive one: a recording is
 * someone else's terminal, and nothing in it is a thing to click.
 */
export function inertLinkHandler(): TerminalLinkHandler {
  return { activate: () => {} };
}
