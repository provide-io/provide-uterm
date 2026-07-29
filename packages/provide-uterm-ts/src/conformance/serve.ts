//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The server role of the live conformance driver.
 *
 * `conformance/live/PROTOCOL.md`: bind an **ephemeral** port, write one line
 * of JSON saying where you are and what token to present, then serve until
 * stdin closes or the process is signalled.
 *
 * The port is the operating system's choice and is reported back rather than
 * agreed in advance. Nothing in the harness, in a scenario, or here may name
 * one — two ports written down are two ports colliding on somebody's machine,
 * and a matrix that fails for that reason says nothing about parity.
 *
 * The token is a real credential. `dev_token` mode mints an HS256 secret and a
 * JWT against it and then rewrites the configuration to `jwt`, so what the
 * harness presents goes through the same validator a production deployment
 * runs. A driver that recognised its own token instead of verifying it would
 * pass every scenario in the matrix while shipping an authentication bypass —
 * which is exactly the kind of thing the matrix exists to catch, and could
 * not catch about itself.
 */

import { bootstrapServer } from "../server/bootstrap.ts";
import { type RunningServer, serveApp } from "../server/node-http.ts";
import { LANGUAGE } from "./client-driver.ts";
import { parseFlags } from "./flags.ts";

/**
 * What this server offers, in the vocabulary scenarios require things in.
 *
 * Empty, and accurately so. The protocol's names are about the hijack
 * surfaces — `hijack.rest`, `hijack.ws` — and this server serves the read half
 * of the session API and the health probes. Claiming one of them would make a
 * scenario that requires it run against a server that cannot answer it, and
 * the cell would fail for the wrong reason.
 */
export const SERVER_CAPABILITIES: readonly string[] = [];

/** The auth mode a `serve` with no `--auth` runs in. */
export const DEFAULT_AUTH_MODE = "dev_token";

/** How the server role is run. Defaults are the real thing. */
export interface ServeOptions {
  /** Where the announcement goes. */
  write?: ((line: string) => void) | undefined;
  /**
   * Resolves when it is time to stop.
   *
   * The harness closes stdin to ask politely and signals to insist; both land
   * here, and a test hands over a promise it resolves itself.
   */
  until?: (() => Promise<void>) | undefined;
  /** How the application is bound. Node's `http` unless a test says otherwise. */
  listen?: typeof serveApp | undefined;
}

/**
 * Wait for the harness to ask this driver to stop.
 *
 * Three ways, because the harness uses all three in order: it closes stdin,
 * then sends `SIGTERM`, then `SIGKILL` — and the third cannot be waited for.
 * Whichever arrives first wins; the rest are unregistered so a driver that
 * stopped does not keep a listener alive and hold the process open.
 */
export function shutdownRequested(input: NodeJS.ReadableStream = process.stdin): Promise<void> {
  return new Promise<void>((resolve) => {
    const done = () => {
      input.off("end", done);
      input.off("close", done);
      process.off("SIGTERM", done);
      process.off("SIGINT", done);
      resolve();
    };
    input.on("end", done);
    input.on("close", done);
    process.on("SIGTERM", done);
    process.on("SIGINT", done);
    // Nothing reads what the harness sends; the stream is only resumed so
    // that its end is noticed at all. A paused stream never ends.
    input.resume();
  });
}

/** The announcement, in the shape the harness reads it. */
export function announcement(server: RunningServer, token: string): string {
  return JSON.stringify({
    role: "server",
    language: LANGUAGE,
    base_url: server.baseUrl,
    token,
    capabilities: [...SERVER_CAPABILITIES],
  });
}

/**
 * Run the server role.
 *
 * @returns The process exit code: zero for a server that ran and stopped when
 *   it was asked to, one for a server that could not start at all.
 */
export async function runServe(argv: readonly string[], options: ServeOptions = {}): Promise<number> {
  const write =
    options.write ??
    ((line: string) => {
      process.stdout.write(`${line}\n`);
    });

  let running: RunningServer;
  let token: string;
  try {
    const flags = parseFlags(argv);
    const bootstrapped = bootstrapServer({ authMode: flags.get("auth") ?? DEFAULT_AUTH_MODE });
    token = bootstrapped.token;
    running = await (options.listen ?? serveApp)(bootstrapped.app);
  } catch (error) {
    // A driver that could not start must say so on stdout rather than dying
    // quietly: the harness waits for a line, and the only thing worse than a
    // failed cell is a hung one.
    write(
      JSON.stringify({
        role: "server",
        language: LANGUAGE,
        status: "error",
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    return 1;
  }

  // Announced only once it is listening. A base URL written before the socket
  // was bound would send the first client to a closed port.
  write(announcement(running, token));
  await (options.until ?? shutdownRequested)();
  await running.close();
  return 0;
}
