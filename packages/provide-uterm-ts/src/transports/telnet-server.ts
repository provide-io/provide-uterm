//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A TCP listener that speaks enough telnet to hand a client over.
 *
 * Port of `provide.uterm.transports.telnet_server`. A client arriving has to
 * be told, before anything else, that this end handles echo and that the
 * connection is full-duplex. Get the preamble wrong and every keystroke
 * appears twice, or the session waits for a go-ahead nobody sends.
 *
 * After the preamble the connection belongs to the handler; this only opens
 * it, says hello, and makes sure it is closed afterwards.
 */

import { createServer, type Server, type Socket } from "node:net";

/** The telnet vocabulary the preamble needs. */
const IAC = 255;
const WILL = 251;
const DO = 253;
const DONT = 254;
const ECHO = 1;
const SGA = 3;
const LINEMODE = 34;
const NAWS = 31;

/** Everything, which is what a terminal server binds to unless told otherwise. */
export const BIND_ALL = "0.0.0.0";

/** The port a telnet server listens on unless told otherwise. */
export const DEFAULT_TELNET_SERVER_PORT = 2102;

/** How long a client is given to take in the preamble. */
export const DEFAULT_NEGOTIATION_DELAY_S = 0.1;

/**
 * The opening sequence every client is sent.
 *
 * This end will echo and will suppress go-ahead, asks the client to suppress
 * it too, turns off the client's own line editing — the session does that —
 * and asks to be told the window size.
 */
export function buildTelnetHandshake(): Uint8Array {
  return Uint8Array.from([IAC, WILL, ECHO, IAC, WILL, SGA, IAC, DO, SGA, IAC, DONT, LINEMODE, IAC, DO, NAWS]);
}

/** What a connection is handed to once it has been greeted. */
export type ConnectionHandler = (socket: Socket) => Promise<void>;

/** What a telnet server is started with. */
export interface TelnetServerOptions {
  /** Called once per connection, after the preamble. */
  handler: ConnectionHandler;
  host?: string;
  port?: number;
  /** How long to pause after the preamble, giving a slow client time to read it. */
  negotiationDelayS?: number;
  /** How the pause is taken. Injected so a test need not spend it. */
  sleep?: (seconds: number) => Promise<void>;
}

/**
 * Where a server will bind, given what it was told.
 *
 * Separated from the binding so the defaults can be checked without opening
 * every interface on a well-known port to look.
 */
export function resolveBind(options: Pick<TelnetServerOptions, "host" | "port">): { host: string; port: number } {
  return { host: options.host ?? BIND_ALL, port: options.port ?? DEFAULT_TELNET_SERVER_PORT };
}

/**
 * The port a listening server ended up on.
 *
 * @throws {Error} When the server is not on a network address — a caller
 *   asking a Unix socket for its port has asked the wrong question.
 */
export function portOf(address: ReturnType<Server["address"]>): number {
  if (address === null || typeof address !== "object") {
    throw new Error("server is not listening on a network address");
  }
  return address.port;
}

/** A running telnet server. */
export interface RunningTelnetServer {
  /** The port it got, which matters when it was asked for zero. */
  readonly port: number;
  /** How many clients are connected. */
  readonly connections: number;
  /** Stop listening and drop every client. */
  close(): Promise<void>;
}

/** Wait for `seconds`. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, seconds * 1000);
  });
}

/**
 * Start listening.
 *
 * @throws {Error} If the address cannot be bound — reported rather than left
 *   to hang.
 */
export async function startTelnetServer(options: TelnetServerOptions): Promise<RunningTelnetServer> {
  const sleep = options.sleep ?? realSleep;
  const delay = options.negotiationDelayS ?? DEFAULT_NEGOTIATION_DELAY_S;
  const sockets = new Set<Socket>();

  const greet = async (socket: Socket): Promise<void> => {
    socket.write(Buffer.from(buildTelnetHandshake()));
    // A pause, not a handshake: the reference does not wait for answers, only
    // gives a slow client a moment to take the options in.
    await sleep(delay);
    try {
      await options.handler(socket);
    } finally {
      // Closed however the handler ended, so a handler that throws does not
      // leave the connection open.
      socket.end();
    }
  };

  const server: Server = createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    // One client's broken socket is that client's problem.
    socket.on("error", () => socket.destroy());
    void greet(socket).catch(() => socket.destroy());
  });

  const bind = resolveBind(options);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(bind.port, bind.host, () => {
      server.removeListener("error", reject);
      resolve();
    });
  });

  return {
    // Asked of the server rather than assumed: a caller that asked for port
    // zero needs the one it actually got.
    port: portOf(server.address()),
    get connections(): number {
      return sockets.size;
    },
    close: async () => {
      for (const socket of [...sockets]) {
        socket.destroy();
      }
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}
