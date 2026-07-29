//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The transport the conformance driver hands to the client library.
 *
 * `conformance/live/PROTOCOL.md` asks for two things this sits between.
 *
 * * **The library performs the call.** What is under test is the client a
 *   consumer would use, so the driver does not hand-roll a request that
 *   happens to agree with it. `HijackClient` takes a transport, so the real
 *   client runs over a real `fetch`.
 * * **The status is observed underneath it.** `HijackClient` answers
 *   `(ok, body)` and drops the status code, so a 401, a 403 and a 404 would
 *   all arrive as the same `ok: false` — three different refusals a matrix
 *   could not tell apart. This transport writes down what came back before
 *   the library shapes it, and shapes nothing itself.
 *
 * Everything it writes down lands in {@link Attempt}: the status, whether the
 * body parsed as JSON, and the error if the request never reached a server at
 * all. Each of the three is an observation the protocol records and the
 * library's answer alone cannot supply.
 */

import type { HijackResponse, HijackTransport } from "../client/hijack-client.ts";

/** Which token a step presents. */
export type AuthMode = "token" | "none" | "bad";

/**
 * The bearer token an `auth: "bad"` step presents.
 *
 * No server issued it and none can: it names itself, so a server log holding
 * it reads as a scenario rather than as an intrusion.
 */
export const BAD_TOKEN = "uterm-live-conformance-token-no-server-issued";

/** What one request turned out to be, beyond what the library reports. */
export interface Attempt {
  /** What the server answered with, or null if it never answered. */
  status: number | null;
  /** Whether the body parsed as JSON. */
  jsonOk: boolean;
  /** Why the request never reached a server, if it did not. */
  error: string | null;
}

/** What the transport is built with. */
export interface FetchTransportOptions {
  /** Where the server driver said it was listening. */
  baseUrl: string;
  /** The token the server driver reported. */
  token: string;
  /** Which token this step presents. */
  auth: AuthMode;
  /** The fetch to use. The runtime's own, unless a test says otherwise. */
  fetchImpl?: typeof fetch | undefined;
}

/**
 * A message for anything thrown.
 *
 * A rejected fetch is usually an `Error`, but a driver that crashed on a
 * thrown string would report nothing at all — and reporting nothing is the
 * one outcome this harness cannot use.
 *
 * The cause is spelt out when there is one, because Node's `fetch` rejects
 * with `fetch failed` and puts the reason underneath: without it, a server
 * that refused the connection, one whose name did not resolve and one whose
 * certificate was wrong are the same line in the matrix.
 */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.cause instanceof Error ? `${error.message}: ${error.cause.message}` : error.message;
  }
  return String(error);
}

/**
 * The `Authorization` header a mode sends, if any.
 *
 * `none` sends no header rather than an empty one: an empty header is still a
 * header, and whether a server tells the two apart is what such a step asks.
 */
export function authHeaders(auth: AuthMode, token: string): Record<string, string> {
  if (auth === "none") {
    return {};
  }
  return { Authorization: `Bearer ${auth === "bad" ? BAD_TOKEN : token}` };
}

/** A `fetch`-backed transport that writes down what came back. */
export class FetchTransport implements HijackTransport {
  /** What the last request turned out to be. */
  readonly attempt: Attempt = { status: null, jsonOk: true, error: null };

  readonly #baseUrl: string;
  readonly #headers: Record<string, string>;
  readonly #fetch: typeof fetch;

  constructor(options: FetchTransportOptions) {
    // Trailing slashes are dropped rather than doubled: `//api/health` is a
    // different path to some servers, and the base URL arrives from whichever
    // server driver is on the other side of the matrix.
    this.#baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.#headers = { Accept: "application/json", ...authHeaders(options.auth, options.token) };
    this.#fetch = options.fetchImpl ?? globalThis.fetch;
  }

  async request(
    method: string,
    path: string,
    options: { json?: unknown; params?: Record<string, unknown> } = {},
  ): Promise<HijackResponse> {
    const url = new URL(`${this.#baseUrl}${path}`);
    for (const [name, value] of Object.entries(options.params ?? {})) {
      url.searchParams.set(name, String(value));
    }

    // Cleared first, so a second step never inherits a first step's
    // observations — a stale 500 would read as this step's answer.
    this.attempt.status = null;
    this.attempt.jsonOk = true;
    this.attempt.error = null;

    const headers: Record<string, string> = { ...this.#headers };
    const init: RequestInit = { method, headers };
    if (options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.json);
    }

    let response: Response;
    try {
      response = await this.#fetch(url.toString(), init);
    } catch (error) {
      // Written down and let through: the library turns it into an answer,
      // and only this layer knows there was no status to record.
      this.attempt.error = errorMessage(error);
      throw error;
    }

    // Read here rather than in `json()`, because the library reads the body
    // synchronously and a stream can only be read once.
    const text = await response.text();
    this.attempt.status = response.status;
    return { status: response.status, text, json: () => this.#parse(text) };
  }

  /** Parse a body, remembering when it could not be parsed. */
  #parse(text: string): unknown {
    try {
      return JSON.parse(text) as unknown;
    } catch (error) {
      this.attempt.jsonOk = false;
      throw error;
    }
  }
}
