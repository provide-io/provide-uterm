//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Recording one hosted session.
 *
 * Port of the recording half of `provide.uterm.server.runtime.HostedSessionRuntime`
 * — `_start_recording`, `_stop_recording`, `flush_recording`, `_log_snapshot`,
 * `_log_send`, `_log_event` and the wire/control loggers — together with the
 * server factory's `build_recording_store` and `_build_recording_redactor`.
 *
 * The entries are the reference's, field for field: a `read` per snapshot the
 * worker sends, a `send` per chunk of input it receives (masked while the last
 * screen ended at a password prompt), and in `wire` mode the raw frames both
 * ways plus every decoded control frame. `session_recording_golden.json` holds
 * a real runtime's output for the same script, so a drift in any of them is a
 * failing test rather than a recording a reader cannot parse.
 */

import { type Annotation, annotationToWire, type PatternDetector, StreamingDetector } from "../annotation/index.ts";
import type { WorkerMessage } from "../connectors/index.ts";
import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import {
  InMemoryRecordingStore,
  LocalFileRecordingStore,
  NullRecordingStore,
  type RecordingStore,
} from "../recording/index.ts";
import { defaultRedactionRules, type Redactor, StreamRedactor } from "../redaction/index.ts";
import { encodeCp437, stripAnsi } from "../screen/index.ts";
import { type ControlChannelMode, SessionLogger } from "../session-logger/index.ts";

/** The `[recording]` section, as the runtime reads it. */
export interface RecordingSettings {
  enabledByDefault: boolean;
  storeType: string;
  directory: string;
  webhookUrl: string | null;
  maxBytes: number;
  controlChannelMode: ControlChannelMode;
  redactSensitive: boolean;
  flushIntervalS: number;
  flushBatchSize: number;
}

/**
 * Read the `[recording]` section of a merged, validated configuration.
 *
 * Every field has a default in `SERVER_CONFIG_DEFAULTS` and has already been
 * through the schema, so nothing here re-validates or falls back.
 */
export function recordingSettingsFrom(section: Readonly<Record<string, unknown>>): RecordingSettings {
  return {
    enabledByDefault: Boolean(section.enabled_by_default),
    storeType: String(section.store_type),
    directory: String(section.directory),
    webhookUrl: (section.webhook_url as string | null | undefined) ?? null,
    maxBytes: Number(section.max_bytes),
    controlChannelMode: section.control_channel_mode as ControlChannelMode,
    redactSensitive: Boolean(section.redact_sensitive),
    flushIntervalS: Number(section.flush_interval_s),
    flushBatchSize: Number(section.flush_batch_size),
  };
}

/**
 * The store the configuration asks for. The reference's `build_recording_store`.
 *
 * The reference's webhook store is not ported. A webhook store with nowhere to
 * deliver falls through to files exactly as it does there; one *with* an
 * address is refused, because recording into nothing while the session's
 * status says it records is the one outcome worse than not starting.
 *
 * @throws {Error} For a webhook store with a delivery address.
 */
export function buildRecordingStore(settings: RecordingSettings): RecordingStore {
  if (settings.storeType === "webhook" && settings.webhookUrl) {
    throw new Error("recording.store_type='webhook' is not supported by this server; use 'local', 'memory' or 'null'");
  }
  if (settings.storeType === "memory") {
    return new InMemoryRecordingStore();
  }
  if (settings.storeType === "null") {
    return new NullRecordingStore();
  }
  return new LocalFileRecordingStore(settings.directory);
}

/** The reference's `_build_recording_redactor`: the default rules, or nothing. */
export function recordingRedactor(enabled: boolean): Redactor | undefined {
  return enabled ? new StreamRedactor(defaultRedactionRules()).redact : undefined;
}

/** What CPython's `str.isspace` counts as whitespace, for a faithful `rstrip`. */
const PY_TRAILING_SPACE = /[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/u;

/** The reference's prompt test, applied to the right-stripped screen. */
const PASSWORD_PROMPT = /(?:password|passphrase)[^\n]*:\s*$/i;

/** Whether a screen ends at a password or passphrase prompt. */
export function atPasswordPrompt(screen: string): boolean {
  return PASSWORD_PROMPT.test(screen.replace(PY_TRAILING_SPACE, ""));
}

/** What a recording is built with beyond its store and settings. */
export interface SessionRecordingOptions {
  /**
   * The detector that annotates what is recorded. Shared across sessions: it
   * is stateless, and each recording wraps it in streams of its own.
   */
  detector?: PatternDetector | undefined;
}

/**
 * A session's recording, across one worker attachment.
 *
 * Every `log*` method is a no-op while nothing is open, which is how the
 * reference's `self._logger is None` checks read: the traffic paths call them
 * unconditionally and only an open recording writes.
 */
export class SessionRecording {
  readonly #sessionId: string;
  readonly #store: RecordingStore;
  readonly #settings: RecordingSettings;
  #logger: SessionLogger | undefined;
  /** Tracked whether or not anything is recorded, as the reference does. */
  #atPasswordPrompt = false;
  readonly #detector: PatternDetector | undefined;
  /**
   * One stream per direction, so a partial match carried across chunks of
   * input is never joined to output, and nothing bleeds between sessions.
   *
   * Built once, with the object, and kept across every recording it opens:
   * the reference holds both on the runtime, which outlives each worker
   * connection, so a match straddling a reconnect is still found.
   */
  readonly #sendStream: StreamingDetector | undefined;
  readonly #readStream: StreamingDetector | undefined;
  /**
   * The reference's `_event_seq`: one per screen and per chunk of input,
   * counted across recordings for the same reason.
   */
  #eventSeq = 0;

  constructor(
    sessionId: string,
    store: RecordingStore,
    settings: RecordingSettings,
    options: SessionRecordingOptions = {},
  ) {
    this.#sessionId = sessionId;
    this.#store = store;
    this.#settings = settings;
    this.#detector = options.detector;
    if (options.detector !== undefined) {
      this.#sendStream = new StreamingDetector(options.detector);
      this.#readStream = new StreamingDetector(options.detector);
    }
  }

  /** Whether a recording is open. */
  get active(): boolean {
    return this.#logger !== undefined;
  }

  /**
   * Open the recording, if this session records and none is open.
   *
   * @param enabled Whether this session records: its own `recording_enabled`,
   *   or the deployment's `enabled_by_default` when it defers.
   */
  async start(enabled: boolean): Promise<void> {
    if (this.#logger !== undefined || !enabled) {
      return;
    }
    const logger = new SessionLogger(this.#store, {
      maxBytes: this.#settings.maxBytes,
      controlChannelMode: this.#settings.controlChannelMode,
      redactor: recordingRedactor(this.#settings.redactSensitive),
      // Both flush knobs, under the logger's own names. The reference once
      // dropped these and recorded every session at the logger's defaults.
      flushIntervalS: this.#settings.flushIntervalS,
      batchSize: this.#settings.flushBatchSize,
    });
    await logger.start(this.#sessionId);
    this.#logger = logger;
  }

  /** Close the recording, writing what is buffered and the closing entry. */
  async stop(): Promise<void> {
    const logger = this.#logger;
    this.#logger = undefined;
    await logger?.stop();
  }

  /** Write what is buffered. The reference's `flush_recording`. */
  async flush(): Promise<void> {
    await this.#logger?.flush();
  }

  /** Log a named event. */
  async logEvent(event: string, data: Record<string, unknown>): Promise<void> {
    await this.#logger?.logEvent(event, data);
  }

  /**
   * Log one frame the worker sends: `_send_outbound_frame`'s logging.
   *
   * In `wire` mode the encoded frame, and the message itself unless it is
   * terminal output; then, for a snapshot, the screen.
   */
  async logOutbound(message: WorkerMessage): Promise<void> {
    const type = message.type;
    // One reading of the data, for the wire entry and the scan alike.
    const data = String(message.data ?? "");
    const logger = this.#logger;
    if (logger !== undefined) {
      const payload = type === "term" ? encodeTerminalData(data) : encodeControlFrame(message);
      await logger.logWire("send", payload);
      if (type !== "term") {
        await logger.logControl("send", message);
      }
    }
    if (type === "snapshot") {
      await this.#logSnapshot(message);
    } else if (type === "term") {
      await this.#scanOutput(data);
    }
  }

  /** Log a raw chunk as the worker received it, in `wire` mode. */
  async logWireRecv(payload: string): Promise<void> {
    await this.#logger?.logWire("recv", payload);
  }

  /** Log a decoded control frame the worker received, in `wire` mode. */
  async logControlRecv(control: Record<string, unknown>): Promise<void> {
    await this.#logger?.logControl("recv", control);
  }

  /** Log typed input, masked at a password prompt. The reference's `_log_send`. */
  async logSend(data: string): Promise<void> {
    const logger = this.#logger;
    if (logger === undefined) {
      return;
    }
    if (this.#atPasswordPrompt) {
      await logger.logSendMasked(encodeCp437(data).length);
    } else {
      await logger.logSend(data);
    }
    this.#eventSeq += 1;
    // The input itself is scanned, masked or not, as the reference scans it.
    await this.#annotate(logger, this.#sendStream?.detect("send", data, this.#eventSeq));
  }

  /** The reference's `_log_snapshot`: note the prompt, then record the screen. */
  async #logSnapshot(message: WorkerMessage): Promise<void> {
    const screen = String(message.screen ?? "");
    this.#atPasswordPrompt = atPasswordPrompt(screen);
    const logger = this.#logger;
    if (logger === undefined) {
      return;
    }
    await logger.logScreen(message, encodeCp437(screen));
    this.#eventSeq += 1;
    // The screen as it is, not stripped: the reference's snapshot path hands
    // the detector the screen text unchanged.
    await this.#annotate(logger, this.#detector?.detect("read", screen, this.#eventSeq));
  }

  /**
   * The reference's `_scan_output`: read-path rules over streamed output, with
   * escape sequences removed, and only while recording.
   *
   * Streamed output here is a `term` message the connector produced — the
   * frame the reference's runtime sends its hub and scans on the way. This
   * server has no worker socket, so it is the message as `inbound` hands it on.
   */
  async #scanOutput(data: string): Promise<void> {
    const logger = this.#logger;
    // An empty frame needs no case of its own: the stream skips empty text.
    if (logger === undefined) {
      return;
    }
    await this.#annotate(logger, this.#readStream?.detect("read", stripAnsi(data), this.#eventSeq));
  }

  /** Record each annotation, in the reference's `to_dict` shape. */
  async #annotate(logger: SessionLogger, annotations: readonly Annotation[] | undefined): Promise<void> {
    for (const annotation of annotations ?? []) {
      await logger.logEvent("annotation", annotationToWire(annotation));
    }
  }
}
