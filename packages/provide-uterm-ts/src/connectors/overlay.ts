//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The screen a remote-endpoint connector shows, and the snapshot of it.
 *
 * The telnet and WebSocket connectors draw the same overlay over whatever
 * their endpoint has sent, and the reference writes it out twice. It is one
 * thing here — with each connector's own corpus proving it still draws what
 * that connector drew.
 */

import { createHash } from "node:crypto";
import type { WorkerMessage } from "./base.ts";

/** The screen the overlay is drawn for. */
export const OVERLAY_COLS = 80;
export const OVERLAY_ROWS = 25;

/** How much output is kept behind the visible screen. */
export const OVERLAY_BUFFER_LIMIT = 32_000;

/** What the overlay says about a session. */
export interface OverlayState {
  sessionId: string;
  displayName: string;
  /** What the session is connected to, as a viewer should read it. */
  upstream: string;
  inputMode: string;
  paused: boolean;
  /** The last thing that happened, shown under the heading. */
  banner: string;
  /** Everything the endpoint has sent, already bounded. */
  buffer: string;
  /** Off means the endpoint's output and nothing else. */
  hubOverlay: boolean;
}

/**
 * Split as Python's `str.splitlines()` does, which drops a trailing break.
 *
 * Nothing splits to nothing rather than to one empty line, which falls out of
 * dropping that trailing break.
 */
export function splitLines(text: string): string[] {
  const lines = text.split(/\r\n|\r|\n/);
  if (lines[lines.length - 1] === "") {
    lines.pop();
  }
  return lines;
}

/**
 * The overlay, then whatever the endpoint has sent, cut to the screen.
 *
 * The last screenful wins, overlay included: once the endpoint has sent more
 * than a screen the header scrolls off with everything else, so a busy session
 * shows only its output and a quiet one says what it is connected to. The
 * reference's behaviour — a port that pinned the header instead would show a
 * different screen than every other port does.
 */
export function overlayScreen(state: OverlayState): string {
  if (!state.hubOverlay) {
    return state.buffer;
  }
  const header = [
    `\x1b[1;35m[${state.displayName} (${state.sessionId})]\x1b[0m`,
    "-".repeat(60),
    `\x1b[32mUpstream:\x1b[0m ${state.upstream}`,
    `\x1b[32mMode:\x1b[0m ${state.inputMode === "open" ? "Shared input" : "Exclusive hijack"}`,
    `\x1b[32mControl:\x1b[0m ${state.paused ? "Paused for hijack" : "Live"}`,
    `\x1b[33m${state.banner}\x1b[0m`,
    "",
  ];
  return [...header, ...splitLines(state.buffer)].slice(-OVERLAY_ROWS).join("\n");
}

/**
 * The snapshot a browser reconciles its own screen against.
 *
 * The cursor is clamped to the screen this is drawn for: one past the last
 * column or row would put a caret somewhere the browser's grid does not have.
 */
export function overlaySnapshot(screen: string, promptId: string, ts: number): WorkerMessage {
  const lines = splitLines(screen);
  const shown = lines.length > 0 ? lines : [""];
  const last = shown[shown.length - 1] as string;
  return {
    type: "snapshot",
    screen,
    cursor: { x: Math.min(last.length, OVERLAY_COLS - 1), y: Math.min(shown.length - 1, OVERLAY_ROWS - 1) },
    cols: OVERLAY_COLS,
    rows: OVERLAY_ROWS,
    screen_hash: createHash("sha256").update(screen, "utf8").digest("hex").slice(0, 16),
    cursor_at_end: true,
    has_trailing_space: false,
    prompt_detected: { prompt_id: promptId },
    ts,
  };
}

/**
 * Keep the newest output and drop the oldest.
 *
 * What a viewer wants to see is what just happened.
 */
export function boundedBuffer(buffer: string, text: string): string {
  return (buffer + text).slice(-OVERLAY_BUFFER_LIMIT);
}

/** The banner a control action leaves behind. */
export function controlBanner(action: string): string {
  if (action === "pause") {
    return "Exclusive control active.";
  }
  if (action === "resume") {
    return "Exclusive control released.";
  }
  if (action === "step") {
    return "Step requested. Awaiting upstream output.";
  }
  // Named rather than ignored silently, so an operator sees that whatever they
  // pressed did nothing.
  return `Ignored control action: ${action}`;
}

/** The banner a mode change leaves behind. */
export function modeBanner(mode: string): string {
  return `Input mode set to ${mode === "open" ? "Shared input" : "Exclusive hijack"}.`;
}

/** How a connector writes a boolean into its analysis, which is Python's way. */
export function pyBool(value: boolean): string {
  return value ? "True" : "False";
}

/** The settings a connector was given, minus any name it does not have. */
export function rejectUnknownConnectorKeys(
  kind: string,
  config: Readonly<Record<string, unknown>>,
  known: ReadonlySet<string>,
): void {
  const unknown = Object.keys(config)
    .filter((key) => !known.has(key))
    .sort();
  if (unknown.length > 0) {
    throw new Error(`unknown ${kind} connector_config keys: [${unknown.map((key) => `'${key}'`).join(", ")}]`);
  }
}
