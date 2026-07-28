//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The WebSocket server.
 *
 * Node has a WebSocket *client* and no server, which is what `ws` supplies.
 * The reference serves these through FastAPI; there is no framework here, so
 * this is the listener and the per-connection lifecycle, and the frame
 * handling above it is the caller's.
 *
 * What it decides is narrow and all of it is about refusing badly: an
 * unrecognised path, an oversized payload, and a handler that throws each end
 * one connection and nothing else.
 */

import { createServer, type Server as HttpServer } from "node:http";
import { WebSocketServer } from "ws";
import { BIND_ALL } from "../defaults/index.ts";

/** The largest message a connection may send, matching the Worker's own cap. */
export const DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576;

/** What a connection can do, as the rest of the port sees it. */
export interface WebSocketConnection {
  /** The path the client connected to. */
  readonly path: string;
  /** The address at the other end, where there is one. */
  readonly peer: readonly [string, number] | undefined;
  send(data: string | Uint8Array): void;
  close(code?: number, reason?: string): void;
  /** Resolves with each message, and with nothing once the connection ends. */
  receive(): Promise<string | Uint8Array | undefined>;
}

/** What a server does with each connection. */
export type WebSocketHandler = (connection: WebSocketConnection) => Promise<void>;

/** How to start the server. */
export interface WebSocketServerOptions {
  host?: string;
  /** The port, or zero to take whatever the system gives. */
  port?: number;
  /** The paths a client may connect to. Any other is refused. */
  paths?: readonly string[];
  maxPayloadBytes?: number;
  onEvent?: (event: string, detail: Record<string, unknown>) => void;
}

/** A server that is listening. */
export interface RunningWebSocketServer {
  readonly host: string;
  readonly port: number;
  close(): Promise<void>;
}

/** What a server runs with, once the defaults are filled in. */
export interface ResolvedWebSocketServerOptions {
  host: string;
  port: number;
  maxPayloadBytes: number;
}

/**
 * Fill in the defaults.
 *
 * Separate from starting, so what a server would bind can be asserted without
 * binding it.
 */
export function resolveWebSocketServerOptions(options: WebSocketServerOptions): ResolvedWebSocketServerOptions {
  return {
    host: options.host ?? BIND_ALL,
    port: options.port ?? 0,
    maxPayloadBytes: options.maxPayloadBytes ?? DEFAULT_MAX_PAYLOAD_BYTES,
  };
}

/**
 * Whether a request's path is one this server serves.
 *
 * An empty list serves everything; a non-empty one is a closed set, matched
 * on the path alone so a query string cannot smuggle a client onto a route it
 * was not offered.
 */
export function servesPath(paths: readonly string[] | undefined, url: string | undefined): boolean {
  if (paths === undefined || paths.length === 0) {
    return true;
  }
  return paths.includes(pathOf(url));
}

/**
 * The path part of a request's target.
 *
 * A query or a fragment is not part of the route: matching on the whole
 * target would let a query string smuggle a client onto a route it was not
 * offered, or keep it off one it was.
 */
export function pathOf(url: string | undefined): string {
  // Splitting a string always yields at least one part, so the first is
  // always present.
  const [beforeQuery] = (url ?? "").split("?") as [string];
  const [path] = beforeQuery.split("#") as [string];
  return path;
}

/**
 * The address at the other end of a request, where there is one.
 *
 * A socket that reports no address is not exempt from anything — it simply
 * has nothing to tell a handler.
 */
export function peerOf(socket: {
  remoteAddress?: string | undefined;
  remotePort?: number | undefined;
}): readonly [string, number] | undefined {
  return typeof socket.remoteAddress === "string" ? [socket.remoteAddress, socket.remotePort ?? 0] : undefined;
}

/** A socket, as `ws` hands one back. */
interface RawSocket {
  on(event: string, listener: (...args: never[]) => void): unknown;
  send(data: string | Uint8Array): void;
  close(code?: number, reason?: string): void;
}

/** Adapt a socket to the pull-based shape the rest of the port reads. */
function connectionFor(
  socket: RawSocket,
  path: string,
  peer: readonly [string, number] | undefined,
): WebSocketConnection {
  const pending: Array<string | Uint8Array> = [];
  let ended = false;
  let wake: (() => void) | undefined;

  socket.on("message", ((data: Buffer, isBinary: boolean) => {
    // Text and binary are different things to the control channel: a frame
    // decoded from the wrong one would be read as the wrong kind entirely.
    pending.push(isBinary ? Uint8Array.from(data) : data.toString("utf8"));
    wake?.();
  }) as (...args: never[]) => void);
  const finish = (): void => {
    ended = true;
    wake?.();
  };
  socket.on("close", finish as (...args: never[]) => void);
  socket.on("error", finish as (...args: never[]) => void);

  return {
    path,
    peer,
    send(data: string | Uint8Array): void {
      socket.send(data);
    },
    close(code?: number, reason?: string): void {
      socket.close(code, reason);
    },
    async receive(): Promise<string | Uint8Array | undefined> {
      while (pending.length === 0 && !ended) {
        await new Promise<void>((resolve) => {
          wake = resolve;
        });
        wake = undefined;
      }
      return pending.shift();
    },
  };
}

/** Start a WebSocket server. */
export async function startWebSocketServer(
  handler: WebSocketHandler,
  options: WebSocketServerOptions = {},
): Promise<RunningWebSocketServer> {
  const { host, port: wantedPort, maxPayloadBytes } = resolveWebSocketServerOptions(options);
  const report = options.onEvent ?? ((): void => undefined);

  const http: HttpServer = createServer((_request, response) => {
    // Anything that is not an upgrade gets a plain refusal rather than a
    // hanging socket.
    response.writeHead(426, { "content-type": "text/plain" });
    response.end("Upgrade Required");
  });
  const sockets = new WebSocketServer({ noServer: true, maxPayload: maxPayloadBytes });

  http.on("upgrade", (request, socket, head) => {
    if (!servesPath(options.paths, request.url)) {
      report("upgrade_refused", { path: request.url });
      // Refused at the HTTP layer: a client that asked for a route this
      // server does not serve never becomes a WebSocket at all.
      socket.write("HTTP/1.1 404 Not Found\r\n\r\n");
      socket.destroy();
      return;
    }
    sockets.handleUpgrade(request, socket, head, (raw) => {
      const connection = connectionFor(raw as unknown as RawSocket, pathOf(request.url), peerOf(request.socket));
      void handler(connection).catch((error: unknown) => {
        // One connection's failure ends that connection and nothing else.
        report("connection_failed", { error: (error as Error).message, path: connection.path });
        connection.close(1011, "handler failed");
      });
    });
  });

  const port = await new Promise<number>((resolve, reject) => {
    http.once("error", reject);
    http.listen(wantedPort, host, () => {
      http.off("error", reject);
      resolve((http.address() as { port: number }).port);
    });
  });
  report("server_started", { host, port });

  return {
    host,
    port,
    close(): Promise<void> {
      return new Promise<void>((resolve) => {
        sockets.close();
        http.close(() => resolve());
      });
    },
  };
}
