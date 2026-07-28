//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The worker's end of the hub connection.
 *
 * Port of the Python module
 * `provide.uterm.server.bridge.worker_link`.
 *
 * The transport itself is the caller's — this owns the decisions: how a
 * manager URL becomes a socket URL, when to give up reconnecting, what each
 * control frame does to the worker, and what happens when the link drops.
 */

import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import { safeInt } from "../pycompat/index.ts";

/**
 * The reconnect ladder, in seconds.
 *
 * Saturates rather than growing without bound: a worker that has been down
 * for hours should keep trying at a steady rate, not drift towards never.
 */
export const RECONNECT_BACKOFF: readonly number[] = [1, 2, 5, 10, 30];

/**
 * Statuses that will never resolve on their own.
 *
 * A rejected token or a wrong path is not a transient fault, and a fleet
 * retrying one forever is a denial of service against its own hub.
 */
const PERMANENT_STATUSES = new Set([401, 403, 404]);

/** Columns assumed when the wire sends nothing usable. */
const DEFAULT_COLS = 80;

/** Rows assumed when the wire sends nothing usable. */
const DEFAULT_ROWS = 25;

/** What the link asks of the worker it is bridging. */
export interface WorkerLinkTarget {
  /** Write operator keystrokes into the session. */
  send(data: string): Promise<void>;
  /** Resize the session's terminal. */
  setSize(cols: number, rows: number): Promise<void>;
  /** Pause or resume the worker's automation. */
  setHijacked(enabled: boolean): Promise<void>;
  /** Let the automation past one checkpoint. */
  requestStep(): Promise<void>;
  /** The current screen, if the session has one. */
  getSnapshot(): Record<string, unknown> | undefined;
}

/** Options for {@link WorkerLink}. */
export interface WorkerLinkOptions {
  /** Identifies this worker to the hub. */
  workerId: string;
  /** Where the hub lives, as an HTTP URL. */
  managerUrl: string;
  /** The worker being bridged. */
  worker: WorkerLinkTarget;
  /** Wall clock in seconds, for outbound timestamps. */
  now?: () => number;
}

/** Handles a control message the link does not know. */
export type MessageHandler = (message: Record<string, unknown>) => Promise<void>;

/**
 * Rewrite a manager URL for a WebSocket connection.
 *
 * Only a scheme at the very start is rewritten, and case-sensitively — a
 * host that merely contains "http" must survive intact, and matching loosely
 * would turn `https://http.example` into something unreachable. An
 * unrecognised scheme is left alone rather than guessed at.
 */
export function toWsUrl(managerUrl: string, path: string): string {
  const base = managerUrl.replace(/\/+$/, "");
  if (base.startsWith("https://")) {
    return `wss://${base.slice("https://".length)}${path}`;
  }
  if (base.startsWith("http://")) {
    return `ws://${base.slice("http://".length)}${path}`;
  }
  return base + path;
}

/**
 * Encode a message for the hub socket.
 *
 * A `term` message is raw terminal data; everything else is a framed control
 * envelope.
 */
export function encodeBridgeFrame(message: Record<string, unknown>): string {
  if (String(message["type"] ?? "") === "term") {
    return encodeTerminalData(String(message["data"] ?? ""));
  }
  return encodeControlFrame(message);
}

/** How long to wait before reconnect attempt `attempt`. */
export function reconnectDelay(attempt: number): number {
  return RECONNECT_BACKOFF[Math.min(attempt, RECONNECT_BACKOFF.length - 1)] as number;
}

/** Whether a connect failure will never resolve on its own. */
export function isPermanentConnectError(status?: number): boolean {
  return status !== undefined && PERMANENT_STATUSES.has(status);
}

/** The worker's end of the hub connection. */
export class WorkerLink {
  readonly #worker: WorkerLinkTarget;
  readonly #now: () => number;
  readonly #handlers = new Map<string, MessageHandler>();
  readonly #listeners: Array<(message: Record<string, unknown>) => void> = [];
  /** Where this link connects, derived once at construction. */
  readonly url: string;

  constructor(options: WorkerLinkOptions) {
    this.#worker = options.worker;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.url = toWsUrl(options.managerUrl, `/ws/worker/${options.workerId}/term`);
  }

  /**
   * Handle a control type the link does not know.
   *
   * Built-in types always win: an application able to shadow `control` could
   * make its own worker unstoppable.
   */
  registerMessageHandler(type: string, handler: MessageHandler): void {
    this.#handlers.set(type, handler);
  }

  /** Receive the messages this link wants sent back to the hub. */
  onSend(listener: (message: Record<string, unknown>) => void): void {
    this.#listeners.push(listener);
  }

  /** Apply one control message from the hub. */
  async handleControl(message: Record<string, unknown>): Promise<void> {
    const type = message["type"];
    if (type === "snapshot_req") {
      await this.#sendSnapshot();
      return;
    }
    if (type === "control") {
      await this.#applyAction(message["action"]);
      return;
    }
    if (type === "resize") {
      // Straight into a PTY ioctl, so a malformed value becomes a sane
      // default rather than reaching the kernel.
      await this.#guard(() =>
        this.#worker.setSize(
          safeInt(message["cols"], DEFAULT_COLS, { minVal: 1 }),
          safeInt(message["rows"], DEFAULT_ROWS, { minVal: 1 }),
        ),
      );
      return;
    }
    const handler = this.#handlers.get(String(type ?? ""));
    if (handler !== undefined) {
      // An application handler is not trusted to be careful; a throw here
      // must not take the connection down.
      await this.#guard(() => handler(message));
    }
  }

  /** Forward operator keystrokes into the session. */
  async handleData(data: string): Promise<void> {
    if (data === "") {
      return;
    }
    await this.#guard(() => this.#worker.send(data));
  }

  /**
   * Clean up after the connection drops.
   *
   * The hub clears its own hijack state, but it cannot send a resume over a
   * closed socket — so the worker has to release itself or it stays paused
   * forever.
   */
  async handleDisconnect(): Promise<void> {
    await this.#setHijacked(false);
  }

  /** Apply a control action, ignoring one the link does not know. */
  async #applyAction(action: unknown): Promise<void> {
    if (action === "pause") {
      await this.#setHijacked(true);
      return;
    }
    if (action === "resume") {
      await this.#setHijacked(false);
      return;
    }
    if (action === "step") {
      await this.#guard(() => this.#worker.requestStep());
    }
  }

  /** Pause or resume, and tell the hub what happened. */
  async #setHijacked(enabled: boolean): Promise<void> {
    await this.#guard(() => this.#worker.setHijacked(enabled));
    // The acknowledgement matters as much as the action: the dashboard shows
    // whether the worker actually paused.
    this.#emit({ type: "status", hijacked: enabled, ts: this.#now() });
  }

  /** Answer a snapshot request, if the worker has a screen to send. */
  async #sendSnapshot(): Promise<void> {
    const snapshot = this.#worker.getSnapshot();
    if (snapshot === undefined) {
      return;
    }
    this.#emit({
      type: "snapshot",
      screen: snapshot["screen"] ?? "",
      cursor: snapshot["cursor"] ?? { x: 0, y: 0 },
      cols: safeInt(snapshot["cols"], DEFAULT_COLS),
      rows: safeInt(snapshot["rows"], DEFAULT_ROWS),
      screen_hash: snapshot["screen_hash"] ?? "",
      cursor_at_end: snapshot["cursor_at_end"] ?? true,
      has_trailing_space: snapshot["has_trailing_space"] ?? false,
      prompt_detected: snapshot["prompt_detected"],
      ts: this.#now(),
    });
  }

  /** Hand a message to whoever is writing to the socket. */
  #emit(message: Record<string, unknown>): void {
    for (const listener of this.#listeners) {
      listener(message);
    }
  }

  /**
   * Run something that touches the session, swallowing failure.
   *
   * The session can die between a frame arriving and being applied, and that
   * must not tear down the connection loop — the reconnect path handles a
   * dead session, this path does not need to.
   */
  async #guard(work: () => Promise<void>): Promise<void> {
    try {
      await work();
    } catch {
      // Nothing better to do than carry on; the transport notices separately.
    }
  }
}
