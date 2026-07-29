//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Taking over a worker's terminal from the outside.
 *
 * Port of `provide.uterm.client.hijack`'s request surface. Every call is a
 * method, a path and a body, and all three are part of the contract a server
 * was written against: a path built wrongly reaches the wrong route, and a
 * field named wrongly is a field the server ignores — which looks to a caller
 * like the call having had no effect.
 *
 * Two rules run through all of it:
 *
 * * **A field nobody set is not sent.** An optional expectation left out is
 *   absent rather than null, because a server reading a null may take it for
 *   an instruction.
 * * **Every answer is a pair**: whether it worked, and the body. A failed call
 *   hands back what the server said rather than raising, so a caller can show
 *   it to somebody.
 */

import { hijackPath, sanitize, sessionPath, workerPath } from "./hijack-guards.ts";

/** What the client got back. */
export interface HijackAnswer {
  /** Whether the server said yes. */
  ok: boolean;
  /** What it said — the parsed body, or the error. */
  body: unknown;
}

/** As much of an HTTP client as this needs. */
export interface HijackTransport {
  request(
    method: string,
    path: string,
    options: { json?: unknown; params?: Record<string, unknown> },
  ): Promise<HijackResponse>;
}

/** As much of a response as this reads. */
export interface HijackResponse {
  status: number;
  /** The parsed body. Throws when there is none to parse. */
  json(): unknown;
  /** The body as it arrived, for when it is not JSON. */
  text: string;
}

/** Told about a call that failed, with the body already stripped of secrets. */
export interface HijackLogger {
  requestFailed(method: string, path: string, status: number | undefined, body: unknown): void;
}

/** A logger that says nothing. */
const SILENT: HijackLogger = { requestFailed: () => {} };

/** Where a worker's routes live unless a caller says otherwise. */
export const DEFAULT_ENTITY_PREFIX = "/worker";

/** How long a lease runs unless a caller says otherwise. */
export const DEFAULT_LEASE_S = 90;

/** How long `send` waits for what it expects. */
export const DEFAULT_SEND_TIMEOUT_MS = 2000;

/** How often `send` looks while it waits. */
export const DEFAULT_POLL_INTERVAL_MS = 120;

/** How long a snapshot waits for something to show. */
export const DEFAULT_SNAPSHOT_WAIT_MS = 1500;

/** How many events are read at once. */
export const DEFAULT_EVENT_LIMIT = 200;

/**
 * How many of a session's events are read at once.
 *
 * Not the same number as a lease's events, and deliberately so: the reference
 * reads a session's in hundreds and a lease's in two hundreds, and a port that
 * shared one constant between them would ask for the wrong count on one of the
 * two paths.
 */
export const DEFAULT_SESSION_EVENT_LIMIT = 100;

/** What the client is built with. */
export interface HijackClientOptions {
  transport: HijackTransport;
  /** Where a worker's routes live. */
  entityPrefix?: string;
  logger?: HijackLogger;
}

/**
 * Drop the fields nobody set, so they are absent rather than null.
 *
 * Both `undefined` and `null` count as unset. The reference has one way of
 * saying nothing and drops it; this runtime has two, and a server reading a
 * null may take it for an instruction either way.
 */
function present<T extends Record<string, unknown>>(body: T): T {
  return Object.fromEntries(Object.entries(body).filter(([, value]) => value !== undefined && value !== null)) as T;
}

/** A client for taking over and driving a worker. */
export class HijackClient {
  readonly #transport: HijackTransport;
  readonly #prefix: string;
  readonly #logger: HijackLogger;

  constructor(options: HijackClientOptions) {
    this.#transport = options.transport;
    this.#prefix = options.entityPrefix ?? DEFAULT_ENTITY_PREFIX;
    this.#logger = options.logger ?? SILENT;
  }

  /**
   * Make one request and read the answer.
   *
   * A transport that fails is an answer too: the error is handed back rather
   * than raised, because a caller driving a terminal needs to show somebody
   * what happened, not unwind.
   */
  async request(
    method: string,
    path: string,
    options: { json?: unknown; params?: Record<string, unknown> } = {},
  ): Promise<HijackAnswer> {
    let response: HijackResponse;
    try {
      response = await this.#transport.request(method, path, options);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.#logger.requestFailed(method, path, undefined, message);
      return { ok: false, body: { error: message } };
    }

    let body: unknown;
    try {
      body = response.json();
    } catch {
      // Not JSON: handed back as it arrived, so a caller can show it.
      body = { raw: response.text };
    }

    if (response.status >= 200 && response.status < 300) {
      return { ok: true, body };
    }
    // Stripped of anything sensitive before it is written down, since a
    // failure body can hold a token.
    this.#logger.requestFailed(method, path, response.status, sanitize(body));
    return { ok: false, body };
  }

  /** Take a lease on a worker. */
  async acquire(
    workerId: string,
    options: { owner?: string | undefined; leaseS?: number | undefined } = {},
  ): Promise<HijackAnswer> {
    return this.request("POST", `${workerPath(this.#prefix, workerId)}/hijack/acquire`, {
      json: { owner: options.owner ?? "operator", lease_s: options.leaseS ?? DEFAULT_LEASE_S },
    });
  }

  /** Keep a lease from expiring. */
  async heartbeat(
    workerId: string,
    hijackId: string,
    options: { leaseS?: number | undefined } = {},
  ): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/heartbeat`, {
      json: { lease_s: options.leaseS ?? DEFAULT_LEASE_S },
    });
  }

  /** Type at a worker, optionally waiting for something to appear. */
  async send(
    workerId: string,
    hijackId: string,
    options: {
      keys: string;
      // Spelt with `undefined` so a caller may pass the field explicitly
      // unset, which is how an optional value usually arrives from a form.
      expectPromptId?: string | undefined;
      expectRegex?: string | undefined;
      timeoutMs?: number | undefined;
      pollIntervalMs?: number | undefined;
    },
  ): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/send`, {
      json: present({
        keys: options.keys,
        timeout_ms: options.timeoutMs ?? DEFAULT_SEND_TIMEOUT_MS,
        poll_interval_ms: options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
        expect_prompt_id: options.expectPromptId,
        expect_regex: options.expectRegex,
      }),
    });
  }

  /** Let a paused worker run one step. */
  async step(workerId: string, hijackId: string): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/step`);
  }

  /** Give the worker back. */
  async release(workerId: string, hijackId: string): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/release`);
  }

  /** Read what the terminal shows. */
  async snapshot(
    workerId: string,
    hijackId: string,
    options: { waitMs?: number | undefined } = {},
  ): Promise<HijackAnswer> {
    return this.request("GET", `${hijackPath(this.#prefix, workerId, hijackId)}/snapshot`, {
      params: { wait_ms: options.waitMs ?? DEFAULT_SNAPSHOT_WAIT_MS },
    });
  }

  /** Read what has happened since a point. */
  async events(
    workerId: string,
    hijackId: string,
    options: { afterSeq?: number | undefined; limit?: number | undefined } = {},
  ): Promise<HijackAnswer> {
    return this.request("GET", `${hijackPath(this.#prefix, workerId, hijackId)}/events`, {
      params: { after_seq: options.afterSeq ?? 0, limit: options.limit ?? DEFAULT_EVENT_LIMIT },
    });
  }

  /** Read what a graphical session shows. */
  async guiScreenshot(workerId: string, hijackId: string): Promise<HijackAnswer> {
    return this.request("GET", `${hijackPath(this.#prefix, workerId, hijackId)}/gui/screenshot`);
  }

  /** Click somewhere in a graphical session. */
  async guiClick(workerId: string, hijackId: string, x: number, y: number, button = "left"): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/gui/click`, {
      json: { x, y, button },
    });
  }

  /** Type into a graphical session. */
  async guiType(workerId: string, hijackId: string, text: string): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/gui/type`, { json: { text } });
  }

  /** Press a key in a graphical session. */
  async guiKey(workerId: string, hijackId: string, keyName: string): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/gui/key`, {
      json: { key_name: keyName },
    });
  }

  /** Drag from one point to another in a graphical session. */
  async guiDrag(
    workerId: string,
    hijackId: string,
    startX: number,
    startY: number,
    endX: number,
    endY: number,
  ): Promise<HijackAnswer> {
    return this.request("POST", `${hijackPath(this.#prefix, workerId, hijackId)}/gui/drag`, {
      json: { start_x: startX, start_y: startY, end_x: endX, end_y: endY },
    });
  }

  /** Say whether a worker takes input from anybody or only from a holder. */
  async setInputMode(workerId: string, mode: string): Promise<HijackAnswer> {
    return this.request("POST", `${workerPath(this.#prefix, workerId)}/input_mode`, { json: { input_mode: mode } });
  }

  /** Drop a worker's connection. */
  async disconnectWorker(workerId: string): Promise<HijackAnswer> {
    return this.request("POST", `${workerPath(this.#prefix, workerId)}/disconnect_worker`);
  }

  /** Whether the server is up. */
  async health(): Promise<HijackAnswer> {
    return this.request("GET", "/api/health");
  }

  /** Every session the server knows. */
  async listSessions(): Promise<HijackAnswer> {
    return this.request("GET", "/api/sessions");
  }

  /** One session. */
  async getSession(sessionId: string): Promise<HijackAnswer> {
    return this.request("GET", sessionPath(sessionId));
  }

  /** What one session's terminal shows. */
  async sessionSnapshot(sessionId: string): Promise<HijackAnswer> {
    return this.request("GET", `${sessionPath(sessionId)}/snapshot`);
  }

  /** What has lately happened in one session. */
  async sessionEvents(sessionId: string, options: { limit?: number | undefined } = {}): Promise<HijackAnswer> {
    return this.request("GET", `${sessionPath(sessionId)}/events`, {
      params: { limit: options.limit ?? DEFAULT_SESSION_EVENT_LIMIT },
    });
  }

  /**
   * Say whether a session takes input from anybody or only from a holder.
   *
   * A session's mode, not a worker's: {@link setInputMode} is the same
   * question asked of the worker behind one, and the two are different routes
   * on the reference server.
   */
  async setSessionMode(sessionId: string, mode: string): Promise<HijackAnswer> {
    return this.request("POST", `${sessionPath(sessionId)}/mode`, { json: { input_mode: mode } });
  }
}
