//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * JSONL session logger for recording terminal sessions.
 *
 * Each entry is one JSON object carrying at minimum `ts`, `event` and `data`.
 * Entries are buffered and flushed in batches, or on a timer, so a chatty
 * session does not turn into one store round-trip per keystroke.
 *
 * Port of the Python module `provide.uterm.session_logger` and the Go package
 * `sessionlogger`.
 */

import type { RecordingEvent, RecordingStore } from "../recording/index.ts";
import { type Redactor, redactText } from "../redaction/index.ts";
import { decodeCp437, encodeCp437 } from "../screen/index.ts";
import { getLogger, type Logger } from "../telemetry/index.ts";

/** Whether control-channel traffic is recorded. */
export type ControlChannelMode = "exclude" | "wire";

/** Direction of a wire or control entry. */
export type Direction = "send" | "recv";

/** Construction options for {@link SessionLogger}. */
export interface SessionLoggerOptions {
  /** Byte budget for the session. Zero means unlimited. */
  maxBytes?: number;
  /** Whether to record raw wire and decoded control frames. */
  controlChannelMode?: ControlChannelMode;
  /** Applied to every logged string before it is written. */
  redactor?: Redactor;
  /** Seconds between periodic flushes. */
  flushIntervalS?: number;
  /** Entries buffered before a flush is forced. */
  batchSize?: number;
  /** Logger for the recorder's own diagnostics. */
  logger?: Logger;
}

/** Base64-encode bytes the way the reference does. */
function toBase64(data: Uint8Array): string {
  return Buffer.from(data).toString("base64");
}

/** Async session recorder writing through a pluggable store. */
export class SessionLogger {
  readonly #store: RecordingStore;
  readonly #maxBytes: number;
  readonly #controlChannelMode: ControlChannelMode;
  readonly #redactor: Redactor | undefined;
  readonly #flushIntervalMs: number;
  readonly #batchSize: number;
  readonly #logger: Logger;

  #sessionId: string | undefined;
  #context: Record<string, string> = {};
  #bytesWritten = 0;
  #quotaWarned = false;
  #buffer: RecordingEvent[] = [];
  #timer: ReturnType<typeof setInterval> | undefined;
  /** Serialises writes so a flush cannot interleave with an append. */
  #chain: Promise<void> = Promise.resolve();

  constructor(store: RecordingStore, options: SessionLoggerOptions = {}) {
    this.#store = store;
    this.#maxBytes = options.maxBytes ?? 0;
    this.#controlChannelMode = options.controlChannelMode ?? "exclude";
    this.#redactor = options.redactor;
    this.#flushIntervalMs = (options.flushIntervalS ?? 5) * 1000;
    this.#batchSize = options.batchSize ?? 100;
    this.#logger = options.logger ?? getLogger("provide.uterm.session_logger");
  }

  /**
   * Run `work` after everything already queued.
   *
   * The reference holds an async lock across every write; this chains them
   * instead, which gives the same ordering guarantee without a lock object.
   */
  #serialise<T>(work: () => Promise<T>): Promise<T> {
    const result = this.#chain.then(work);
    // Swallow on the chain only: the caller still sees the rejection, but one
    // failed write must not poison every write after it.
    this.#chain = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  /** Begin a recording session. */
  async start(sessionId: string): Promise<void> {
    this.#sessionId = sessionId;
    await this.#store.startSession(sessionId, { started_at: Date.now() / 1000 });
    const meta = await this.#store.recordingMeta(sessionId);
    const size = Number(meta.size_bytes);
    this.#bytesWritten = Number.isFinite(size) ? size : 0;
    this.#timer = setInterval(() => {
      void this.flush().catch((error: unknown) => {
        // A transient store failure must not stop the periodic flusher: the
        // batch stays buffered, so the next tick retries it. Logged at warn
        // rather than error because it is expected and retried.
        this.#logger.warn(
          { event: "session_logger_periodic_flush_failed", session_id: this.#sessionId, error: String(error) },
          "periodic flush failed",
        );
      });
    }, this.#flushIntervalMs);
    // A recorder must never hold the process open on its own.
    this.#timer.unref?.();
  }

  /** Finalise the recording session. */
  async stop(): Promise<void> {
    if (this.#timer !== undefined) {
      clearInterval(this.#timer);
      this.#timer = undefined;
    }
    await this.flush();
    if (this.#sessionId !== undefined) {
      await this.#store.endSession(this.#sessionId);
    }
  }

  /** Log sent keystrokes. */
  async logSend(keys: string): Promise<void> {
    const redacted = this.#redact(keys);
    await this.#write("send", { keys: redacted, bytes_b64: toBase64(encodeCp437(redacted)) });
  }

  /** Log a credential send without capturing the value. */
  async logSendMasked(byteCount: number): Promise<void> {
    await this.#write("send", {
      keys: "***",
      bytes_b64: toBase64(new Uint8Array(Buffer.from("***", "ascii"))),
      masked: true,
      byte_count: byteCount,
    });
  }

  /** Log a screen snapshot alongside the raw bytes that produced it. */
  async logScreen(snapshot: Record<string, unknown>, raw: Uint8Array): Promise<void> {
    const rawText = this.#redact(decodeCp437(raw));
    await this.#write("read", {
      ...(this.#redactValue(snapshot) as Record<string, unknown>),
      raw: rawText,
      raw_bytes_b64: toBase64(encodeCp437(rawText)),
    });
  }

  /** Log an arbitrary named event. */
  async logEvent(event: string, data: Record<string, unknown>): Promise<void> {
    await this.#write(event, data);
  }

  /** Log a raw wire chunk, when wire-mode recording is enabled. */
  async logWire(direction: Direction, text: string): Promise<void> {
    if (this.#controlChannelMode !== "wire") {
      return;
    }
    const redacted = this.#redact(text);
    await this.#write(`wire_${direction}`, {
      text: redacted,
      bytes_b64: toBase64(new Uint8Array(Buffer.from(redacted, "utf-8"))),
    });
  }

  /** Log a decoded control frame, when wire-mode recording is enabled. */
  async logControl(direction: Direction, control: Record<string, unknown>): Promise<void> {
    if (this.#controlChannelMode !== "wire") {
      return;
    }
    await this.#write(`control_${direction}`, { control });
  }

  /**
   * Set metadata attached to subsequent entries.
   *
   * Values are stringified, matching the reference, so a numeric context
   * value arrives on the wire as text rather than varying by call site.
   */
  setContext(context: Record<string, unknown>): void {
    this.#context = Object.fromEntries(Object.entries(context).map(([key, value]) => [String(key), String(value)]));
  }

  /** Clear the metadata context. */
  clearContext(): void {
    this.#context = {};
  }

  /** Flush buffered entries to the store. */
  flush(): Promise<void> {
    return this.#serialise(() => this.#flushUnlocked());
  }

  /**
   * Write the buffer to the store.
   *
   * The buffer is cleared only after the store accepts the batch: if the
   * store throws, the batch stays buffered for the next attempt rather than
   * being lost.
   */
  async #flushUnlocked(): Promise<void> {
    if (this.#buffer.length === 0 || this.#sessionId === undefined) {
      return;
    }
    const batch = [...this.#buffer];
    await this.#store.appendEvents(this.#sessionId, batch);
    this.#buffer = this.#buffer.slice(batch.length);
  }

  /** Buffer one entry, flushing when the batch is full. */
  #write(event: string, data: Record<string, unknown>): Promise<void> {
    return this.#serialise(async () => {
      if (this.#maxBytes > 0 && this.#bytesWritten >= this.#maxBytes) {
        if (!this.#quotaWarned) {
          this.#quotaWarned = true;
          this.#logger.warn(
            { event: "session_logger_quota_reached", session_id: this.#sessionId },
            "further writes suppressed",
          );
        }
        return;
      }

      const record: RecordingEvent = { ts: Date.now() / 1000, event, data };
      if (this.#sessionId !== undefined) {
        record.session_id = this.#sessionId;
      }
      if (Object.keys(this.#context).length > 0) {
        record.ctx = { ...this.#context };
      }

      this.#buffer.push(record);
      this.#bytesWritten += JSON.stringify(record).length + 1;

      if (this.#buffer.length >= this.#batchSize) {
        await this.#flushUnlocked();
      }
    });
  }

  /** Apply the configured redactor to a string. */
  #redact(value: string): string {
    return redactText(value, this.#redactor);
  }

  /** Apply the redactor to every string inside a value, recursively. */
  #redactValue(value: unknown): unknown {
    if (typeof value === "string") {
      return this.#redact(value);
    }
    if (Array.isArray(value)) {
      return value.map((item) => this.#redactValue(item));
    }
    if (typeof value === "object" && value !== null) {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, this.#redactValue(item)]));
    }
    return value;
  }
}
