//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The SSH server.
 *
 * Port of `start_ssh_server` from `provide.uterm.transports.ssh`, over `ssh2`
 * where the reference uses asyncssh.
 *
 * Everything it decides was ported before it and is composed here: the
 * permissive-auth refusal and per-IP limit from `ssh-policy`, the key store
 * from `host-key`, and the byte adapters from `transports/ssh`. What is left
 * is the wiring — which the reference marks no-cover as a live-connection
 * callback, and which this port stands up on an ephemeral port and connects
 * to for real.
 */

import { Server, type Server as SshServerType } from "ssh2";
import { BIND_ALL, SSH_PORT } from "../defaults/index.ts";
import { type SshProcess, SshStreamReader, SshStreamWriter } from "../transports/index.ts";
import { getOrCreateHostKey } from "./host-key.ts";
import {
  assertAuthenticationConfigured,
  ConnectionLimiter,
  type ConnectionSlot,
  DEFAULT_MAX_CONNECTIONS_PER_IP,
  validatePassword,
  validatePublicKey,
} from "./ssh-policy.ts";

/** What a session does with its two halves. */
export type SshConnectionHandler = (reader: SshStreamReader, writer: SshStreamWriter) => Promise<void>;

/** How to start the server. */
export interface SshServerOptions {
  /** The bind address. Anything but loopback needs a validator or an opt-in. */
  host?: string;
  /** The port, or zero to take whatever the system gives. */
  port?: number;
  /** Where the host key lives. */
  hostKeyDir: string;
  maxConnectionsPerIp?: number;
  credentialsValidator?: (user: string, password: string) => boolean;
  publicKeyValidator?: (user: string, key: unknown) => boolean;
  /** Explicitly permit accepting any credential on a public bind. */
  allowUnauthenticated?: boolean;
  /** Told when a connection is refused or a session fails. */
  onEvent?: (event: string, detail: Record<string, unknown>) => void;
}

/** A server that is listening. */
export interface RunningSshServer {
  readonly host: string;
  /** The port actually bound, which is what a caller needs when it asked for zero. */
  readonly port: number;
  close(): Promise<void>;
}

/** What a server runs with, once the defaults are filled in. */
export interface ResolvedSshServerOptions {
  host: string;
  port: number;
  maxConnectionsPerIp: number;
}

/**
 * Fill in the defaults.
 *
 * Separate from starting, so what a server would bind can be asserted without
 * binding it — a test that took the default port would have to hold 2222,
 * which is neither reliable nor its business.
 */
export function resolveSshServerOptions(options: SshServerOptions): ResolvedSshServerOptions {
  return {
    host: options.host ?? BIND_ALL,
    port: options.port ?? SSH_PORT,
    maxConnectionsPerIp: options.maxConnectionsPerIp ?? DEFAULT_MAX_CONNECTIONS_PER_IP,
  };
}

/** What `ssh2` reports about the far end of a connection. */
export interface ConnectionInfo {
  ip?: string;
  port?: number;
}

/**
 * The peer address, or nothing where there is none to read.
 *
 * A connection with no address is not exempt from the per-IP limit — the
 * limiter counts it under its own bucket — but it has no address to report to
 * a session.
 */
export function peerFrom(info: ConnectionInfo | undefined): readonly [string, number] | undefined {
  return typeof info?.ip === "string" ? [info.ip, info.port ?? 0] : undefined;
}

/** A duplex channel, as `ssh2` hands one back. */
interface Channel {
  write(data: Uint8Array): boolean;
  on(event: string, listener: (...args: never[]) => void): unknown;
  once(event: string, listener: (...args: never[]) => void): unknown;
  end(): void;
  exit(code: number): void;
  close(): void;
}

/**
 * Adapt a channel to the process shape the byte adapters expect.
 *
 * The reader is pull-based and the channel is push-based, so arriving data is
 * queued: a session that is slow to read must not lose what arrived while it
 * was busy.
 */
function processFor(channel: Channel, peer: readonly [string, number] | undefined): SshProcess {
  const pending: Uint8Array[] = [];
  let ended = false;
  let wake: (() => void) | undefined;

  const arrived = (chunk: Uint8Array): void => {
    pending.push(chunk);
    wake?.();
  };
  const finished = (): void => {
    ended = true;
    wake?.();
  };
  channel.on("data", arrived as (...args: never[]) => void);
  channel.once("end", finished as (...args: never[]) => void);
  channel.once("close", finished as (...args: never[]) => void);

  return {
    stdin: {
      async read(): Promise<unknown> {
        while (pending.length === 0 && !ended) {
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
          wake = undefined;
        }
        // Nothing left and the far end has gone: the adapter reads that as
        // the end of the stream.
        return pending.shift();
      },
    },
    stdout: {
      write(data: Uint8Array): void {
        channel.write(data);
      },
      async drain(): Promise<void> {
        // `ssh2` buffers internally and reports back-pressure through the
        // return of `write`; there is no separate flush to await.
      },
    },
    exit(code: number): void {
      channel.exit(code);
      channel.end();
    },
    close(): void {
      channel.close();
    },
    getExtraInfo(): unknown {
      // The name is not consulted: the writer is the only caller and asks
      // for the peer alone, having already decided that anything else takes
      // the caller's default.
      return peer;
    },
  };
}

/**
 * Start an SSH server.
 *
 * @throws {PermissiveAuthError} When it would accept any credential on a
 *   non-loopback bind without an explicit opt-in.
 * @throws {InsecureHostKeyError} When an existing host key's permissions are
 *   wrong.
 */
export async function startSshServer(
  handler: SshConnectionHandler,
  options: SshServerOptions,
): Promise<RunningSshServer> {
  const { host, port: wantedPort, maxConnectionsPerIp } = resolveSshServerOptions(options);
  // Checked first, before a key is generated or a socket opened: a server
  // that must not start should not leave anything behind.
  assertAuthenticationConfigured({
    host,
    hasPasswordValidator: options.credentialsValidator !== undefined,
    hasPublicKeyValidator: options.publicKeyValidator !== undefined,
    allowUnauthenticated: options.allowUnauthenticated === true,
  });

  const hostKey = getOrCreateHostKey(options.hostKeyDir);
  const limiter = new ConnectionLimiter(maxConnectionsPerIp);
  const report = options.onEvent ?? ((): void => undefined);

  const server: SshServerType = new Server({ hostKeys: [hostKey] }, (client, info) => {
    const peer = peerFrom(info as ConnectionInfo | undefined);

    const slot: ConnectionSlot | undefined = limiter.admit(peer);
    if (slot === undefined) {
      report("connection_rejected", { reason: "per-ip limit", peer: peer?.[0] });
      client.end();
      return;
    }
    // Given back however the connection ends, including a failed handshake —
    // otherwise one aborted attempt would hold a slot for the process's life.
    client.once("close", () => slot.release());

    client.on("authentication", (context) => {
      const accepted =
        context.method === "password"
          ? validatePassword(context.username, context.password, options.credentialsValidator)
          : context.method === "publickey"
            ? validatePublicKey(context.username, context.key, options.publicKeyValidator)
            : // Any other method — keyboard-interactive, none — is accepted
              // only where nothing is being checked at all, which
              // `assertAuthenticationConfigured` has already confined to a
              // loopback bind or an explicit opt-in.
              options.credentialsValidator === undefined && options.publicKeyValidator === undefined;
      if (accepted) {
        context.accept();
        return;
      }
      report("authentication_rejected", { user: context.username, method: context.method });
      context.reject();
    });

    client.on("session", (accept) => {
      const session = accept();
      const start = (accepter: () => Channel): void => {
        const channel = accepter();
        const process = processFor(channel, peer);
        void handler(new SshStreamReader(process), new SshStreamWriter(process)).catch((error: unknown) => {
          // A handler that throws ends its own session and nothing else: one
          // session's failure must not take the server down.
          report("session_failed", { error: (error as Error).message, peer: peer?.[0] });
          new SshStreamWriter(process).close();
        });
      };
      // A pty request precedes a shell and carries only the terminal size,
      // which this server does not act on; accepting it lets the shell
      // follow.
      session.on("pty", (accept) => accept?.());
      session.on("shell", start as (...args: never[]) => void);
      session.on("exec", start as (...args: never[]) => void);
    });

    client.on("error", (error: Error) => report("client_error", { error: error.message, peer: peer?.[0] }));
  });

  const port = await new Promise<number>((resolve, reject) => {
    server.once("error", reject);
    server.listen(wantedPort, host, () => {
      server.off("error", reject);
      resolve((server.address() as { port: number }).port);
    });
  });
  report("server_started", { host, port });

  return {
    host,
    port,
    close(): Promise<void> {
      return new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}
