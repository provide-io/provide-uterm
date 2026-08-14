//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Builder helpers for hijack-bridge wire frames.
 *
 * `generated/frames.ts` is the single source of truth for frame *shapes* —
 * it is produced from the same Pydantic models as the browser frontend's
 * copy, in the same `scripts/codegen_frames.py` run, so the two consumers
 * cannot disagree about the wire. This module builds those frames.
 *
 * The subtlety worth stating: builders differ in whether a null field
 * survives. `snapshot`, `analysis` and `hijack_state` keep theirs, because
 * the frontend reads `prompt_detected`, `raw_tail`, `raw`, `owner` and
 * `lease_expires_at` directly off the frame and would see `undefined`
 * instead of `null` if they were dropped. The rest omit absent fields to
 * keep frames lean.
 *
 * Port of the Python module `provide.uterm.server.bridge.frames` and the Go
 * package `frames`.
 */

import type {
  AnalysisFrame,
  ErrorFrame,
  HeartbeatAckFrame,
  HijackStateFrame,
  PongFrame,
  SnapshotFrame,
  TermFrame,
  WorkerConnectedFrame,
  WorkerDisconnectedFrame,
} from "./generated/frames.ts";

/** Options shared by every timestamped builder. */
export interface TimestampOptions {
  /** Frame timestamp in seconds. Defaults to now. */
  ts?: number;
}

/** Current time in seconds, matching Python's `time.time()`. */
function now(): number {
  return Date.now() / 1000;
}

/** Build an `error` frame. */
export function makeErrorFrame(message: string): ErrorFrame {
  return { type: "error", message };
}

/** Build a `pong` frame. */
export function makePongFrame(options: TimestampOptions = {}): PongFrame {
  return { type: "pong", ts: options.ts ?? now() };
}

/** Build a `heartbeat_ack` frame. */
export function makeHeartbeatAckFrame(leaseExpiresAt: number, options: TimestampOptions = {}): HeartbeatAckFrame {
  return { type: "heartbeat_ack", lease_expires_at: leaseExpiresAt, ts: options.ts ?? now() };
}

/** Build a `worker_connected` frame. */
export function makeWorkerConnectedFrame(workerId: string, options: TimestampOptions = {}): WorkerConnectedFrame {
  return { type: "worker_connected", worker_id: workerId, ts: options.ts ?? now() };
}

/** Build a `worker_disconnected` frame. */
export function makeWorkerDisconnectedFrame(workerId: string, options: TimestampOptions = {}): WorkerDisconnectedFrame {
  return { type: "worker_disconnected", worker_id: workerId, ts: options.ts ?? now() };
}

/** Build a `term` frame carrying raw terminal data. */
export function makeTermFrame(data: string, options: TimestampOptions = {}): TermFrame {
  return { type: "term", data, ts: options.ts ?? now() };
}

/** Inputs to {@link makeSnapshotFrame}. */
export interface SnapshotFrameArgs {
  screen: string;
  cursor: Record<string, number>;
  cols: number;
  rows: number;
  screenHash: string;
  cursorAtEnd: boolean;
  hasTrailingSpace: boolean;
  /** Kept on the wire even when null — the frontend reads it directly. */
  promptDetected: Record<string, unknown> | null;
  ts: number;
  /** Kept on the wire even when null, for the same reason. */
  rawTail?: string | null;
  /**
   * Reader-loop ingest counters from the worker's own session, counted before
   * any emulator work. They separate "no bytes ever reached that process" from
   * "bytes arrived and the emulator never reflected them" — two failures
   * needing opposite fixes that look identical from the screen alone.
   *
   * Kept on the wire even when null: the Python builder emits them under
   * `exclude_none=False`, and this builder is checked against its output.
   */
  chunksRead?: number | null;
  bytesRead?: number | null;
  /** Manager-assigned causal sequence; absent on worker-originated snapshots. */
  eventSeq?: number;
}

/** Build a `snapshot` frame. */
export function makeSnapshotFrame(args: SnapshotFrameArgs): SnapshotFrame {
  return {
    type: "snapshot",
    screen: args.screen,
    cursor: args.cursor,
    cols: args.cols,
    rows: args.rows,
    screen_hash: args.screenHash,
    cursor_at_end: args.cursorAtEnd,
    has_trailing_space: args.hasTrailingSpace,
    prompt_detected: args.promptDetected,
    raw_tail: args.rawTail ?? null,
    chunks_read: args.chunksRead ?? null,
    bytes_read: args.bytesRead ?? null,
    ts: args.ts,
    ...(args.eventSeq === undefined ? {} : { event_seq: args.eventSeq }),
  } as SnapshotFrame;
}

/** Inputs to {@link makeAnalysisFrame}. */
export interface AnalysisFrameArgs {
  formatted: string;
  /** Kept on the wire even when null — the frontend reads it directly. */
  raw: unknown;
  ts?: number;
}

/** Build an `analysis` frame. */
export function makeAnalysisFrame(args: AnalysisFrameArgs): AnalysisFrame {
  return { type: "analysis", formatted: args.formatted, raw: args.raw, ts: args.ts ?? now() } as AnalysisFrame;
}

/** Inputs to {@link makeHijackStateFrame}. */
export interface HijackStateFrameArgs {
  hijacked: boolean;
  /** Kept on the wire even when null. */
  owner: string | null;
  /** Kept on the wire even when null. */
  leaseExpiresAt: number | null;
  inputMode: string;
}

/** Build a `hijack_state` frame. This frame carries no timestamp. */
export function makeHijackStateFrame(args: HijackStateFrameArgs): HijackStateFrame {
  return {
    type: "hijack_state",
    hijacked: args.hijacked,
    owner: args.owner,
    lease_expires_at: args.leaseExpiresAt,
    input_mode: args.inputMode,
  } as HijackStateFrame;
}

/**
 * Build a `hello` frame.
 *
 * Hello payloads carry arbitrary capability flags that are not part of the
 * schema, so they pass through unmodelled. `type` is stamped last so a
 * caller cannot displace it.
 */
export function makeHelloFrame(payload: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    mcp_supported: true,
    vnc_supported: true,
    ...payload,
    type: "hello",
  };
}

/** Fill in the default type and timestamp on a worker-supplied status frame. */
export function coerceWorkerStatusFrame(payload: Record<string, unknown>): Record<string, unknown> {
  const frame = { ...payload };
  frame.type ??= "status";
  frame.ts ??= now();
  return frame;
}
