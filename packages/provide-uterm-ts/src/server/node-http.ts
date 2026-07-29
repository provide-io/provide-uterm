//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Running a {@link ServerApp} on Node's `http`.
 *
 * The only file in `src/server/` that touches a runtime, and the reason it is
 * not re-exported from the package's default entry: everything else here
 * speaks `Request` and `Response`, which a Worker has too, and a Worker that
 * pulled in `node:http` by importing the server would not start.
 *
 * The port is always the operating system's choice unless a caller names one.
 * A conformance driver binds zero and reports back what it got — nothing in
 * this repository may write a port down, because two of them written down are
 * two of them colliding on somebody's machine.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { SERVER_HOST } from "../defaults/index.ts";
import type { ServerApp } from "./app.ts";

/** Where a server ended up listening, and how to stop it. */
export interface RunningServer {
  host: string;
  port: number;
  /** The base URL a client should be pointed at. */
  baseUrl: string;
  /** Stop listening and wait until nothing is left open. */
  close(): Promise<void>;
}

/** How to listen. */
export interface ListenOptions {
  /** Which address to bind. Loopback unless a caller insists otherwise. */
  host?: string | undefined;
  /** Which port to bind. Zero — the operating system's choice — by default. */
  port?: number | undefined;
}

/** Turn one of Node's requests into the `Request` the app speaks. */
export async function toRequest(message: IncomingMessage, baseUrl: string): Promise<Request> {
  const headers = new Headers();
  for (const [name, value] of Object.entries(message.headers)) {
    if (value === undefined) {
      continue;
    }
    // A header sent more than once arrives as a list; each value is added
    // rather than joined, so a receiver reads them as they were sent.
    for (const one of Array.isArray(value) ? value : [value]) {
      headers.append(name, one);
    }
  }

  const method = message.method ?? "GET";
  // A GET or a HEAD may not carry a body, and constructing a `Request` with
  // one throws — so the body is only read for the methods that can have it.
  const init: RequestInit = { method, headers };
  if (method !== "GET" && method !== "HEAD") {
    const chunks: Buffer[] = [];
    for await (const chunk of message) {
      chunks.push(Buffer.from(chunk as Buffer));
    }
    init.body = Buffer.concat(chunks);
  }
  return new Request(new URL(message.url ?? "/", baseUrl), init);
}

/** Write a `Response` back out through Node's stream. */
export async function writeResponse(response: Response, out: ServerResponse): Promise<void> {
  const headers: Record<string, string | string[]> = {};
  for (const [name, value] of response.headers.entries()) {
    headers[name] = value;
  }
  out.writeHead(response.status, headers);
  out.end(Buffer.from(await response.arrayBuffer()));
}

/**
 * Serve an application until it is closed.
 *
 * Resolves once the socket is bound, so a caller can announce the port it was
 * given — which is the whole shape of the conformance protocol's `serve`.
 */
export function serveApp(app: ServerApp, options: ListenOptions = {}): Promise<RunningServer> {
  const host = options.host ?? SERVER_HOST;
  const server: Server = createServer((message, out) => {
    // The base is only there to make a URL out of a path; the address a
    // caller used is theirs, and nothing here reads it back.
    void handle(app, message, out, `http://${host}`);
  });

  return new Promise<RunningServer>((resolve, reject) => {
    // A bind that fails must reject rather than leave the caller waiting for
    // a `listening` that will never come.
    server.once("error", reject);
    server.listen(options.port ?? 0, host, () => {
      server.removeListener("error", reject);
      const address = server.address() as AddressInfo;
      resolve({
        host: address.address,
        port: address.port,
        baseUrl: `http://${host}:${address.port}`,
        close: () =>
          new Promise<void>((done) => {
            // Sockets a client left open would otherwise hold the process up
            // long past the harness's patience.
            server.closeAllConnections();
            server.close(() => {
              done();
            });
          }),
      });
    });
  });
}

/** One request, answered — or refused with a 500 nobody has to guess at. */
async function handle(app: ServerApp, message: IncomingMessage, out: ServerResponse, base: string): Promise<void> {
  try {
    // The socket's own address, not `X-Forwarded-For`: what the rate limit is
    // keyed on has to be something the caller cannot choose for itself.
    const request = await toRequest(message, base);
    await writeResponse(await app.handle(request, message.socket.remoteAddress), out);
  } catch {
    // A handler that threw is this server's fault, and the connection must
    // still be answered: a client left hanging cannot tell a crash from a
    // slow response.
    out.writeHead(500, { "content-type": "application/json" });
    out.end(JSON.stringify({ detail: "Internal Server Error" }));
  }
}
