//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A real pseudo-terminal, behind the connector's backend interface.
 *
 * The connector reads when it is polled, but node-pty pushes; the queue here
 * is what bridges the two. It is bounded for the same reason the connector's
 * own buffer is: a child that writes faster than anybody polls must cost a
 * fixed amount of memory, not an unbounded one.
 */

import type { IPty } from "node-pty";
import type { PtyBackend } from "./connector.ts";

/** How much unread output is kept before the oldest is dropped. */
export const QUEUE_CAP = 1 << 20;

/** What to run, and how. */
export interface SpawnOptions {
  command: string;
  args?: readonly string[];
  cols?: number;
  rows?: number;
  cwd?: string;
  env?: Record<string, string>;
}

/**
 * Start a shell and give the connector something to read from.
 *
 * @throws {Error} If a terminal cannot be allocated.
 */
export async function spawnNodePty(options: SpawnOptions): Promise<PtyBackend & { pid: number }> {
  // Imported here rather than at module load: node-pty is a native module, and
  // a runtime without it should still be able to use everything else.
  const pty = await import("node-pty");

  const child: IPty = pty.spawn(options.command, [...(options.args ?? [])], {
    name: "xterm-256color",
    cols: options.cols ?? 80,
    rows: options.rows ?? 24,
    ...(options.cwd === undefined ? {} : { cwd: options.cwd }),
    env: { ...process.env, ...options.env } as Record<string, string>,
  });

  let queue = new Uint8Array();
  let alive = true;

  child.onData((chunk) => {
    // node-pty hands over a string it decoded itself; the connector decodes,
    // so this goes back to bytes rather than being decoded twice.
    const bytes = Buffer.from(chunk, "utf8");
    const combined = new Uint8Array(queue.length + bytes.length);
    combined.set(queue);
    combined.set(bytes, queue.length);
    // Oldest first: what a viewer wants is the newest output, and dropping the
    // newest would hide the thing that just went wrong.
    queue = combined.length > QUEUE_CAP ? combined.slice(-QUEUE_CAP) : combined;
  });

  child.onExit(() => {
    alive = false;
  });

  return {
    pid: child.pid,
    read(): Uint8Array | undefined {
      if (queue.length > 0) {
        const taken = queue;
        queue = new Uint8Array();
        return taken;
      }
      // Nothing queued: still running means nothing *yet*, which is not the
      // ending an empty array would report. Output that arrived before the
      // child exited is still handed over first.
      return alive ? undefined : new Uint8Array();
    },
    write(data: Uint8Array): void {
      if (!alive) {
        // Matching a real terminal, which fails once the child is gone: the
        // connector treats that as the session ending.
        throw Object.assign(new Error("the child has exited"), { code: "EIO" });
      }
      child.write(new TextDecoder().decode(data));
    },
    isAlive(): boolean {
      return alive;
    },
    async close(): Promise<void> {
      if (alive) {
        child.kill();
        alive = false;
      }
    },
  };
}
