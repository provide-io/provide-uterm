//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A telnet listener that hands each connection to a session upstream.
 *
 * Port of `provide.uterm.gateway._telnet_gateway`. Telnet is plaintext and
 * unauthenticated by nature, so the only thing standing between it and the
 * network is where it is bound: a non-loopback address needs an explicit
 * opt-in, and without one starting the listener is refused rather than
 * quietly exposing a shell.
 *
 * Each connection is negotiated with {@link IacNegotiator} before anything is
 * forwarded, so the upstream session is opened knowing what terminal is at the
 * far end.
 */

import { createServer, type Server, type Socket } from "node:net";
import { IacNegotiator } from "./iac.ts";
import { isLoopbackBind } from "./ssh-policy.ts";

/** The address a telnet gateway binds to unless told otherwise. */
export const DEFAULT_TELNET_HOST = "127.0.0.1";

/** The port it listens on unless told otherwise. */
export const DEFAULT_TELNET_PORT = 2112;

/** How long a client is given to answer the opening questions. */
export const DEFAULT_NEGOTIATE_TIMEOUT_S = 0.4;

/** A session upstream, once a client has been identified. */
export interface UpstreamSession {
  /** Send what the client typed. */
  send(data: Uint8Array): Promise<void>;
  /** Stop. */
  close(): Promise<void>;
}

/** What a gateway is configured with. */
export interface TelnetGatewayOptions {
  /** Where the sessions live. */
  wsUrl: string;
  /** The colour mode used when a client says nothing about its terminal. */
  colorMode?: string;
  /** Whether to ask the client what it is before forwarding anything. */
  iacNegotiate?: boolean;
  /** How long to wait for an answer. */
  iacNegotiateTimeoutS?: number;
  /**
   * Whether this may listen anywhere but loopback.
   *
   * Telnet carries no authentication and no encryption, so binding it to a
   * routable address puts an unauthenticated shell on the network. Refused
   * unless somebody says, in as many words, that something else is guarding
   * it.
   */
  allowUnauthenticated?: boolean;
  /** Opens a session upstream once the client has been identified. */
  connect(details: {
    colormode: string | undefined;
    term: string;
    env: Record<string, string>;
  }): Promise<UpstreamSession>;
  /** How the wait for negotiation is taken. Injected so a test need not spend it. */
  sleep?: (seconds: number) => Promise<void>;
}

/** Wait for `seconds`. */
function realSleep(seconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, seconds * 1000);
  });
}

/**
 * Check a bind address before anything is opened.
 *
 * @throws {Error} When this would put an unauthenticated telnet listener on a
 *   routable address without somebody having said so.
 */
export function assertBindAllowed(host: string, allowUnauthenticated: boolean): void {
  if (!allowUnauthenticated && !isLoopbackBind(host)) {
    throw new Error(
      "refusing to start an unauthenticated telnet gateway on a non-loopback bind address; " +
        "set allowUnauthenticated only when this listener is protected by another access-control layer",
    );
  }
}

/**
 * The port a listening server ended up on.
 *
 * @throws {Error} When the server is not listening on a network address — a
 *   caller asking a Unix socket for its port has asked the wrong question.
 */
export function portOf(address: ReturnType<Server["address"]>): number {
  if (address === null || typeof address !== "object") {
    throw new Error("telnet gateway is not listening on a network address");
  }
  return address.port;
}

/** A running listener. */
export interface RunningTelnetGateway {
  /** The port it actually got, which matters when it was asked for zero. */
  readonly port: number;
  /** How many clients are connected. */
  readonly connections: number;
  /** Stop listening and drop every client. */
  close(): Promise<void>;
}

/** A telnet listener in front of a session server. */
export class TelnetGateway {
  readonly #options: TelnetGatewayOptions;

  constructor(options: TelnetGatewayOptions) {
    this.#options = options;
  }

  /**
   * Start listening.
   *
   * @throws {Error} Before binding, when the address is not one an
   *   unauthenticated listener may use.
   */
  async start(host: string = DEFAULT_TELNET_HOST, port: number = DEFAULT_TELNET_PORT): Promise<RunningTelnetGateway> {
    // Checked before the socket is opened, so a refused configuration never
    // holds a port even briefly.
    assertBindAllowed(host, this.#options.allowUnauthenticated === true);

    const sockets = new Set<Socket>();
    const server: Server = createServer((socket) => {
      sockets.add(socket);
      socket.on("close", () => sockets.delete(socket));
      // A connection that fails is one client's problem, not the listener's.
      socket.on("error", () => socket.destroy());
      void this.#serve(socket).catch(() => socket.destroy());
    });

    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, host, () => {
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

  /** Negotiate with one client, then join it to a session. */
  async #serve(socket: Socket): Promise<void> {
    const negotiator = new IacNegotiator();
    const sleep = this.#options.sleep ?? realSleep;
    const pending: Uint8Array[] = [];

    if (this.#options.iacNegotiate !== false) {
      socket.write(Buffer.from(negotiator.startBytes()));
      // Bounded: a client that never answers is still a client, and it gets
      // the default colour mode rather than a connection that hangs.
      const deadline = sleep(this.#options.iacNegotiateTimeoutS ?? DEFAULT_NEGOTIATE_TIMEOUT_S);
      await Promise.race([deadline, this.#negotiate(socket, negotiator, pending)]);
    }

    const session = await this.#options.connect({
      colormode: negotiator.derivedColormode() ?? this.#options.colorMode,
      term: negotiator.term,
      env: { ...negotiator.env },
    });

    // Anything the client sent while it was being asked still belongs to it.
    for (const chunk of pending) {
      await session.send(chunk);
    }

    socket.on("data", (chunk: Buffer) => {
      const { reply, cleaned } = negotiator.feed(new Uint8Array(chunk));
      if (reply.length > 0) {
        socket.write(Buffer.from(reply));
      }
      if (cleaned.length > 0) {
        void session.send(cleaned);
      }
    });
    socket.on("close", () => {
      void session.close();
    });
  }

  /** Read until the client has answered both questions. */
  #negotiate(socket: Socket, negotiator: IacNegotiator, pending: Uint8Array[]): Promise<void> {
    return new Promise<void>((resolve) => {
      const onData = (chunk: Buffer): void => {
        const { reply, cleaned } = negotiator.feed(new Uint8Array(chunk));
        if (reply.length > 0) {
          socket.write(Buffer.from(reply));
        }
        if (cleaned.length > 0) {
          // Held rather than dropped: a client that types before it is asked
          // has still typed.
          pending.push(cleaned);
        }
        if (negotiator.done()) {
          socket.removeListener("data", onData);
          resolve();
        }
      };
      socket.on("data", onData);
    });
  }
}
