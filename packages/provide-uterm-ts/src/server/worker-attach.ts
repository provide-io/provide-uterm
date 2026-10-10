//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Attaching a started session's connector to the hub as a worker.
 *
 * This is the piece that makes a lease mean something. The hub arbitrates over
 * *workers*: `tryAcquireRest` refuses `no_worker` unless a socket is attached,
 * `waitForSnapshot` asks the worker for a screen and waits for one to come
 * back, and a `pause` frame is what actually stops the session. A session that
 * had merely been started would satisfy none of that, and a route that granted
 * a lease anyway would be lying about who is driving the terminal.
 *
 * ## The transport is in-process, and everything either side of it is real
 *
 * The reference's `HostedSessionRuntime` dials its own server over a
 * WebSocket: `ws://.../ws/worker/{id}/term`. This port binds no WebSocket
 * server, so the same two ends are joined directly instead — the hub writes an
 * encoded frame, this decodes it, and {@link WorkerLink} — the real worker-side
 * protocol, ported and tested in `bridge/worker-link.ts` — decides what it
 * means. What comes back goes through the hub's real inbound handling.
 *
 * So the *frames* are real (encoded and decoded through `control-channel`),
 * the *worker* is real (a live connector, started by the runtime), and the
 * *decisions* on both ends are the ported ones. What is missing is a network
 * hop between two things running in one process, which was never where any of
 * the behaviour lived: the reference loops back to itself over localhost.
 *
 * A `snapshot_req` therefore travels the whole way — hub, frame, decoder,
 * link, connector, frame, hub — and lands in `state.lastSnapshot` before the
 * poll that is waiting for it reads again.
 */

import { WorkerLink, type WorkerLinkTarget } from "../bridge/index.ts";
import type { SessionConnector, WorkerMessage } from "../connectors/index.ts";
import { ControlFrameDecoder } from "../control-channel/index.ts";
import { makeSnapshotFrame } from "../frames/index.ts";
import { type InputMode, safeFloat, type WorkerSocket } from "../hub/index.ts";
import { safeInt } from "../pycompat/index.ts";
import { getLogger, type Logger } from "../telemetry/index.ts";
import type { SessionHub } from "./session-hub.ts";
import type { SessionRecording } from "./session-recording.ts";

/** Columns assumed when a connector's snapshot says nothing usable. */
const DEFAULT_COLS = 80;

/** Rows assumed when a connector's snapshot says nothing usable. */
const DEFAULT_ROWS = 25;

/**
 * How long to wait after a poll that found nothing, in milliseconds.
 *
 * The reference's run loop sleeps 0.05 s when `poll_messages()` comes back
 * empty, so a connector with no wait of its own is not polled in a hot loop.
 */
export const POLL_IDLE_MS = 50;

/**
 * How long to wait after a poll that threw, in milliseconds, by how many have
 * thrown in a row. The reference's run-loop backoff, `[0.25, 0.5, 1.0, 2.0,
 * 5.0]` seconds, staying on the last step.
 */
export const POLL_ERROR_BACKOFF_MS: readonly number[] = [250, 500, 1000, 2000, 5000];

/** Wait `ms` without holding the process open for it. */
function idle(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms).unref();
  });
}

/** What went wrong, as a recorded `runtime_error` carries it. */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** A connector attached to the hub, and the way to take it back off. */
export interface AttachedWorker {
  /** Detach from the hub. The connector itself is the runtime's to stop. */
  detach(): Promise<void>;
}

/** Options for {@link attachConnector}. Defaults are the real clock. */
export interface AttachOptions {
  /** Wall seconds, as every timestamp on this wire is in. */
  now?: (() => number) | undefined;
  /**
   * The session's recording, written to as traffic crosses the attachment.
   *
   * Where the reference's runtime logs: every frame the worker sends, every
   * chunk it receives, and the start of the run. The recording decides for
   * itself whether anything is written, so a session that does not record is
   * handed one that is not open, or none.
   */
  recording?: SessionRecording | undefined;
  /** How the poll loop waits. A real, unreferenced timer unless a test says otherwise. */
  sleep?: ((ms: number) => Promise<void>) | undefined;
  /**
   * Told the text of every failure the attachment survives — a poll that
   * threw, or traffic the hub or the recording could not take. The
   * reference's run loop stores the same text as the runtime's `_last_error`.
   */
  onError?: ((error: string) => void) | undefined;
  /**
   * Where those failures are logged. The reference's runtime module logger
   * unless a test says otherwise: a failure nobody can see is how a recording
   * that stopped writing goes unnoticed.
   */
  logger?: Logger | undefined;
}

/**
 * Build the wire frame for a worker's snapshot message.
 *
 * The reference builds it in `websockets_worker._build_worker_frame` on the
 * way *in*, not in the connector on the way out — which is why `raw_tail` is
 * on every stored snapshot even though no connector sets it, and why a
 * connector that sent nonsense for `cols` gets a usable number rather than
 * poisoning every client that renders the screen.
 */
/**
 * Coerce to an integer but preserve absence as `null`.
 *
 * Ports `_opt_int` from websockets_worker.py, and deliberately NOT `safeInt`,
 * which substitutes a default: a worker predating the ingest counters has to
 * stay distinguishable from one reporting a genuine zero. Collapsing the two
 * would make "nothing was ever read" and "this build cannot tell you" the same
 * value — the exact ambiguity these counters exist to remove.
 *
 * Non-integers are rejected rather than rounded, matching the reference's
 * `isinstance(value, int)`. Booleans fall out for free here: `typeof true` is
 * `"boolean"`, where Python needed an explicit `not isinstance(value, bool)`
 * because bool subclasses int.
 */
function optionalInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

export function workerSnapshotFrame(message: WorkerMessage, now: number): Record<string, unknown> {
  return makeSnapshotFrame({
    screen: String(message.screen ?? ""),
    cursor: (message.cursor ?? { x: 0, y: 0 }) as Record<string, number>,
    cols: safeInt(message.cols, DEFAULT_COLS, { minVal: 1 }),
    rows: safeInt(message.rows, DEFAULT_ROWS, { minVal: 1 }),
    screenHash: String(message.screen_hash ?? ""),
    cursorAtEnd: Boolean(message.cursor_at_end ?? true),
    hasTrailingSpace: Boolean(message.has_trailing_space ?? false),
    promptDetected: (message.prompt_detected ?? null) as Record<string, unknown> | null,
    rawTail: (message.raw_tail ?? null) as string | null,
    // Forwarded from the worker, never synthesised: these count what the
    // worker's own reader loop ingested before any emulator work, so only it
    // can know them. The reference forwards them the same way
    // (`_opt_int(msg.get("chunks_read"))` in websockets_worker.py). A worker
    // predating the counters omits both and they stay null, which reads as
    // "unknown" rather than as a wrong zero.
    chunksRead: optionalInt(message.chunks_read),
    bytesRead: optionalInt(message.bytes_read),
    ts: safeFloat(message.ts, now),
  }) as unknown as Record<string, unknown>;
}

/**
 * Attach `connector` to `hub` as the worker for `sessionId`.
 *
 * @param mode The input mode the session is configured in. Sent as the
 *   worker's hello would send it, because the hub's own `inputMode` — not the
 *   session definition's — is what an acquire is refused against.
 */
export async function attachConnector(
  hub: SessionHub,
  sessionId: string,
  connector: SessionConnector,
  mode: InputMode,
  options: AttachOptions = {},
): Promise<AttachedWorker> {
  const now = options.now ?? (() => Date.now() / 1000);
  const recording = options.recording;
  const sleep = options.sleep ?? idle;
  const logger = options.logger ?? getLogger("provide.uterm.server.runtime");
  /** Set on detach. The poll loop checks it after every await. */
  let detached = false;
  /** The last screen the connector produced, for the link's synchronous read. */
  let lastSnapshot: WorkerMessage | undefined;

  /**
   * Hand everything a connector produced to the hub.
   *
   * A snapshot is recorded and logged; anything else is only broadcast. The
   * reference's `_dispatch_worker_frame`, with its ordering: stored before it
   * is announced, so nothing can observe the event without the screen.
   */
  async function inbound(messages: readonly WorkerMessage[]): Promise<void> {
    for (const message of messages) {
      if (String(message.type ?? "") !== "snapshot") {
        await hub.router.broadcast(sessionId, message);
      } else {
        lastSnapshot = message;
        const frame = workerSnapshotFrame(message, now());
        const committed = await hub.commitSnapshotEvent(sessionId, frame, socket);
        if (committed !== undefined) {
          await hub.router.broadcast(sessionId, committed, socket, Number(committed.event_seq));
        }
      }
      // Logged once it has been handed over, as the reference logs a frame
      // after its socket write: the worker's message, not the hub's frame.
      await recording?.logOutbound(message);
    }
  }

  /**
   * The connector, in the shape {@link WorkerLink} drives a worker through.
   *
   * The link is the *decisions* — which control action pauses, what a resize
   * does to a malformed number, that a snapshot request is answered only when
   * there is a screen. This is only the connector underneath them.
   */
  const target: WorkerLinkTarget = {
    send: async (data) => {
      await inbound(await connector.handleInput(data));
    },
    setSize: async () => {
      // The reference shell connector has no terminal to resize either: its
      // screen is a fixed 80x25 transcript. A connector that did would take
      // the size here.
    },
    setHijacked: async (enabled) => {
      await inbound(await connector.handleControl(enabled ? "pause" : "resume"));
    },
    requestStep: async () => {
      await inbound(await connector.handleControl("step"));
    },
    getSnapshot: () => lastSnapshot as Record<string, unknown> | undefined,
  };

  /**
   * Report a failure the attachment carries on past. Never throws.
   *
   * The reference's run loop catches whatever ended its connection — a poll,
   * a socket write, a recording store that could not write (disk full,
   * EACCES) — logs `hosted_session_runtime_failed`, keeps the text as
   * `_last_error`, and records a `runtime_error`. Here, where nothing is
   * awaiting the work that failed, a rejection that escaped would be an
   * unhandled one, and Node exits on those: a full disk would take every
   * session down with it. So the same three things are done, and the
   * `runtime_error` entry is best-effort — the store that just failed is the
   * one it would be written to, and its failing again is logged, not thrown.
   */
  async function report(error: unknown): Promise<void> {
    const text = errorText(error);
    logger.warn({ session_id: sessionId, error: text }, "hosted_session_runtime_failed");
    options.onError?.(text);
    try {
      await recording?.logEvent("runtime_error", { error: text });
    } catch (recordError) {
      logger.warn({ session_id: sessionId, error: errorText(recordError) }, "hosted_session_recording_failed");
    }
  }

  const link = new WorkerLink({ workerId: sessionId, managerUrl: "http://in-process", worker: target, now });
  // What the link decides to send back travels the same path a worker's own
  // socket write would: straight into the hub's inbound handling. Nothing
  // awaits it, so a failure on the way is reported here rather than left to
  // become an unhandled rejection.
  link.onSend((message) => {
    void inbound([message]).catch(report);
  });

  const decoder = new ControlFrameDecoder();
  /**
   * The hub's end of the wire.
   *
   * Everything the hub sends a worker is either a DLE/STX control frame or raw
   * terminal bytes, and the decoder is what tells them apart — the same
   * decoder a socket reader would feed.
   */
  const socket: WorkerSocket = {
    sendText: async (payload) => {
      // Recorded as it arrives and before it is acted on, in the reference's
      // order: the raw chunk, then each thing it decoded to.
      await recording?.logWireRecv(payload);
      for (const chunk of decoder.feed(payload)) {
        if (chunk.kind === "data") {
          await recording?.logSend(chunk.data);
          await link.handleData(chunk.data);
        } else {
          await recording?.logControlRecv(chunk.control);
          await link.handleControl(chunk.control);
        }
      }
    },
  };

  hub.registerWorker(sessionId, socket, mode);
  // The reference's `runtime_started`, written as its worker connects and
  // before the first screen it sends.
  await recording?.logEvent("runtime_started", { session_id: sessionId });
  // The reference's worker sends a snapshot as soon as it connects, which is
  // why `GET /api/sessions/{id}/snapshot` answers with a screen before anybody
  // has typed anything. Sent through the same inbound path as every later one.
  await inbound([await connector.getSnapshot()]);

  /**
   * Poll the connector for output it produced on its own.
   *
   * The reference's `_bridge_session` polls `poll_messages()` in its run loop
   * beside the socket read, sends whatever comes back through the same path
   * as every other frame — broadcast, recorded, annotated — and sleeps 50 ms
   * when nothing did. Input reaches this worker through the socket's own
   * handler, so the loop has nothing to multiplex with and simply waits for
   * each poll: the reference's 0.5 s timeout exists only to get back to its
   * socket read, and cancelling a poll here would lose whatever it had read.
   *
   * A poll that throws ends the reference's connection, which it records as a
   * `runtime_error` and retries on its backoff. There is no connection here
   * to drop, so the error is reported and the poll retried on the same
   * schedule; one that succeeds starts the schedule over. Handing what a poll
   * returned on — the hub's broadcast, the recording's write — fails the same
   * way and is treated the same way: in the reference both run inside the
   * one `try` its run loop catches.
   *
   * Detaching stops it. A poll still in flight then is not waited for — the
   * reference cancels it — and what it returns afterwards is dropped.
   */
  async function poll(): Promise<void> {
    let failures = 0;
    while (!detached) {
      try {
        const messages = await connector.pollMessages();
        failures = 0;
        if (detached) {
          return;
        }
        if (messages.length === 0) {
          await sleep(POLL_IDLE_MS);
          continue;
        }
        await inbound(messages);
      } catch (error) {
        if (detached) {
          return;
        }
        await report(error);
        await sleep(POLL_ERROR_BACKOFF_MS[Math.min(failures, POLL_ERROR_BACKOFF_MS.length - 1)] as number);
        failures += 1;
      }
    }
  }
  void poll();

  return {
    detach: async () => {
      detached = true;
      hub.connections.deregisterWorker(sessionId, socket);
      await hub.pruneIfIdle(sessionId);
    },
  };
}
