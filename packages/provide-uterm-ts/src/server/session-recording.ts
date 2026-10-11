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

/**
 * Whether a screen ends at a password or passphrase prompt.
 *
 * Takes the screen as *text*: the caller strips escape codes first, as the
 * reference's `_log_snapshot` does, because a rendered row ends in a reset
 * that would otherwise sit after the colon and hide the prompt.
 */
export function atPasswordPrompt(screen: string): boolean {
  return PASSWORD_PROMPT.test(screen.replace(PY_TRAILING_SPACE, ""));
}

/**
 * An escape sequence cut off at the end of a streamed chunk: a bare ESC, or a
 * CSI (ESC [) whose parameter/intermediate bytes have not yet reached a final
 * byte. The reference's `_INCOMPLETE_ESCAPE_TAIL` (runtime_helpers.py), whose
 * grammar is exactly that of `strip_ansi`'s `_ANSI_ESCAPE_RE`: anything that
 * pattern would strip once complete, and nothing else, is held back.
 */
const INCOMPLETE_ESCAPE_TAIL = /\x1b(?:\[[0-?]*[ -/]*)?$/;

/**
 * Longest tail held back between chunks, in characters, ESC included. The
 * reference's `_MAX_ESCAPE_CARRY`: a real sequence is a handful of bytes, and
 * past this the "sequence" is not one (or is hostile), so it is released into
 * the text rather than carried forever.
 */
export const MAX_ESCAPE_CARRY = 64;

/**
 * Split `text` into `[complete, carry]`, where `carry` is an unterminated
 * trailing escape sequence. The reference's `_split_incomplete_escape`.
 *
 * `stripAnsi` sees one chunk at a time, so `"\x1b[1"` + `"msudo"` would strip
 * to `"\x1b[1"` (no final byte, left alone) and `"msudo"`, and `\bsudo\b`
 * would miss. The caller prepends the carry to the next chunk.
 *
 * The reference finds the last ESC with `rfind` and `fullmatch`es the pattern
 * from there. Searching for the end-anchored pattern is the same test: only
 * the LAST ESC can start a match that reaches the end, because CSI parameter
 * and intermediate bytes never include ESC — so the match, when there is one,
 * starts exactly where `rfind` would have. And the tail it measures is all
 * ASCII, so its length is the same in UTF-16 units as in the reference's code
 * points.
 */
export function splitIncompleteEscape(text: string): [string, string] {
  const match = INCOMPLETE_ESCAPE_TAIL.exec(text);
  if (match === null || text.length - match.index > MAX_ESCAPE_CARRY) {
    return [text, ""];
  }
  return [text.slice(0, match.index), text.slice(match.index)];
}

/**
 * Bound on the per-recording set of read-path annotation keys. The
 * reference's `_MAX_READ_ANNOTATION_KEYS`: past it the set is cleared and
 * starts over, and the worst case is that a snapshot repeats an annotation the
 * stream already recorded once more than 1024 distinct read-path matches ago.
 */
export const MAX_READ_ANNOTATION_KEYS = 1024;

/**
 * Identity of a read-path annotation for snapshot dedupe: rule + matched text.
 * The reference's `_read_annotation_key`.
 *
 * An annotation carries no rule id; `label` is the rule's category label and
 * `description` is the rule's own template formatted with the match (cut at
 * 80 characters). So the pair names the rule and, for every rule whose
 * template embeds `{match}`, the matched text. Credential rules deliberately
 * never embed the match, so for them the key is the rule alone.
 */
export function readAnnotationKey(annotation: Annotation): string {
  return `${annotation.label}\x00${annotation.description}`;
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
  /**
   * An escape sequence the last streamed chunk ended inside of, held back and
   * prepended to the next chunk before `stripAnsi`: see {@link #scanOutput}.
   * The reference's `_escape_carry`.
   */
  #escapeCarry = "";
  /**
   * Read-path rules run twice over the same output — once over each streamed
   * `term` chunk, once over each snapshot screen — so every read-path
   * annotation recorded is remembered here (by {@link readAnnotationKey}) and
   * the snapshot path skips one already in it. The reference's
   * `_read_annotation_keys`.
   *
   * This and {@link #escapeCarry} are scoped to one recording and reset with
   * it, in {@link stop} — unlike the streams and the sequence above, which the
   * reference keeps across recordings.
   */
  readonly #readAnnotationKeys = new Set<string>();

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

  /**
   * Close the recording, writing what is buffered and the closing entry.
   *
   * The escape carry and the read-path keys belong to the recording that just
   * ended, as the reference's `_stop_recording` resets them: the next one
   * annotates what it sees afresh, and does not begin with a stale
   * half-sequence. Reset before the final write, so a store that fails it
   * still leaves the next recording clean.
   */
  async stop(): Promise<void> {
    const logger = this.#logger;
    this.#logger = undefined;
    this.#escapeCarry = "";
    this.#readAnnotationKeys.clear();
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
    // Read as text, as the reference now does. A rendered screen carries SGR
    // codes and ends each row with a reset, which hides a trailing
    // "Password:" from the prompt check — so the next thing typed would be
    // recorded in the clear — and splits a styled match from the read-path
    // rules. The screen itself is still recorded as it was sent.
    const text = stripAnsi(screen);
    this.#atPasswordPrompt = atPasswordPrompt(text);
    const logger = this.#logger;
    if (logger === undefined) {
      return;
    }
    await logger.logScreen(message, encodeCp437(screen));
    this.#eventSeq += 1;
    for (const annotation of this.#detector?.detect("read", text, this.#eventSeq) ?? []) {
      // The snapshot path DEDUPES: a match the stream (or an earlier snapshot
      // of the same screen) already recorded is skipped, so neither a
      // stream+snapshot pair nor a run of identical snapshots records it twice.
      const key = readAnnotationKey(annotation);
      if (this.#readAnnotationKeys.has(key)) {
        continue;
      }
      this.#rememberReadAnnotation(key);
      await logger.logEvent("annotation", annotationToWire(annotation));
    }
  }

  /** Add `key` to the bounded read-path set, clearing it first when full. */
  #rememberReadAnnotation(key: string): void {
    if (this.#readAnnotationKeys.size >= MAX_READ_ANNOTATION_KEYS) {
      this.#readAnnotationKeys.clear();
    }
    this.#readAnnotationKeys.add(key);
  }

  /**
   * The reference's `_scan_output`: read-path rules over streamed output, with
   * escape sequences removed, and only while recording.
   *
   * Streamed output here is a `term` message the connector produced — the
   * frame the reference's runtime sends its hub and scans on the way. This
   * server has no worker socket, so it is the message as `inbound` hands it on.
   *
   * An escape sequence split across chunks (`...\x1b[1` | `msudo ...`) would
   * leave `msudo` behind if each chunk were stripped alone, so an
   * unterminated trailing sequence is held back in {@link #escapeCarry} and
   * prepended to the next chunk (bounded by {@link MAX_ESCAPE_CARRY}).
   *
   * The stream path NEVER suppresses a match — a command run twice is
   * annotated twice — but remembers each one so the snapshot path, which sees
   * the same text again, does not record it a second time.
   */
  async #scanOutput(data: string): Promise<void> {
    const logger = this.#logger;
    const stream = this.#readStream;
    // The reference also returns on an empty frame. That needs no case of its
    // own here: an empty chunk leaves the carry as it was (the split hands the
    // same tail back) and the stream skips empty text.
    if (logger === undefined || stream === undefined) {
      return;
    }
    const [text, carry] = splitIncompleteEscape(this.#escapeCarry + data);
    this.#escapeCarry = carry;
    for (const annotation of stream.detect("read", stripAnsi(text), this.#eventSeq)) {
      this.#rememberReadAnnotation(readAnnotationKey(annotation));
      await logger.logEvent("annotation", annotationToWire(annotation));
    }
  }

  /** Record each annotation, in the reference's `to_dict` shape. */
  async #annotate(logger: SessionLogger, annotations: readonly Annotation[] | undefined): Promise<void> {
    for (const annotation of annotations ?? []) {
      await logger.logEvent("annotation", annotationToWire(annotation));
    }
  }
}
