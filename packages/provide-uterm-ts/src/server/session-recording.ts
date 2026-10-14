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

import { randomBytes } from "node:crypto";
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
import { encodeCp437, MAX_ESCAPE_CARRY, splitIncompleteEscape, stripAnsi } from "../screen/index.ts";
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
  /** Seconds between recorded screens of a changing session; 0 records only screens sent to a viewer. */
  screenIntervalS: number;
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
    screenIntervalS: Number(section.screen_interval_s),
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
 * The chunk-boundary escape carry lives with `stripAnsi`, built from the same
 * grammar constants, so a change to what `stripAnsi` strips keeps the carry
 * correct. Re-exported under the names this module has always offered, as
 * the reference keeps `_split_incomplete_escape` as an alias.
 */
export { MAX_ESCAPE_CARRY, splitIncompleteEscape };

/**
 * Bound on each per-recording set of read-path annotation keys. The
 * reference's `_MAX_READ_ANNOTATION_KEYS`: past it the set is cleared and
 * starts over — simple, and the worst case is one repeated (or, for the
 * snapshot-only set, one extra) annotation more than 1024 distinct read-path
 * matches after the first.
 */
export const MAX_READ_ANNOTATION_KEYS = 1024;

/**
 * Add `key` to `keys`, first clearing the set when it is full. The
 * reference's `_add_bounded`.
 */
export function addBounded(keys: Set<string>, key: string): void {
  if (keys.size >= MAX_READ_ANNOTATION_KEYS) {
    keys.clear();
  }
  keys.add(key);
}

/**
 * Identity of a read-path annotation for stream/snapshot dedupe. The
 * reference's `_read_annotation_key`.
 *
 * The detector's {@link Annotation.matchKey}: a SHA-256 of the rule id and the
 * full matched text. It names the rule AND the exact text matched — so two
 * different AWS keys, which share a label and a description (credential rules
 * never embed the match), are two keys — and it is a digest, so the dedupe
 * sets never hold a raw secret. An annotation the detector did not make has
 * none, and keys as the empty string, as the reference's default does — and
 * an empty key is never deduped: it is recorded every time and added to
 * neither set, since it would otherwise collapse every such annotation into
 * one.
 */
export function readAnnotationKey(annotation: Annotation): string {
  return annotation.matchKey ?? "";
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
  /**
   * Names this session's recordings to the store. A session id outlives the
   * session it names -- a PAM capture id is a pid, and pids are reused -- so a
   * new connection continues the recording only under the same instance, and
   * a later session reusing the id starts its own. Port of the reference's
   * `HostedSessionRuntime._recording_instance`.
   */
  readonly #instance = randomBytes(16).toString("hex");
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
   * `term` chunk, once over each snapshot screen. Dedupe between the two is
   * keyed by {@link readAnnotationKey} (a digest; never the raw text). The
   * reference's rule, from `HostedSessionRuntime.__init__`, exactly:
   *
   * - `#readAnnotationKeys`: every key recorded in this recording, by either
   *   path.
   * - `#snapshotOnlyKeys`: keys the SNAPSHOT path recorded since the last
   *   processed stream chunk.
   * - An EMPTY key is never deduped: always recorded, added to neither set.
   * - Snapshot match: key in `#readAnnotationKeys` → skip. Otherwise record
   *   it and add the key to BOTH sets.
   * - Stream chunk (non-empty, while recording): for each match, key in
   *   `#snapshotOnlyKeys` → remove it and skip. Otherwise record it and add
   *   the key to `#readAnnotationKeys`. AFTER the chunk's matches are
   *   handled, `#snapshotOnlyKeys` is CLEARED: a pending key dedupes only the
   *   one stream chunk that immediately follows the snapshot(s).
   *
   * So stream-then-snapshot records once, snapshot-then-next-chunk records
   * once, a run of identical snapshots records once, and a command streamed
   * twice is annotated twice; a pending key can never suppress a later,
   * genuinely new match. If the snapshot's text reaches the stream only in a
   * LATER chunk it is recorded a second time — a duplicate is preferred over
   * a missed annotation.
   *
   * Both are bounded ({@link addBounded}), scoped to one recording and reset
   * with it, in {@link stop} — as {@link #escapeCarry} is, and unlike the
   * streams and the sequence above, which the reference keeps across
   * recordings.
   */
  readonly #readAnnotationKeys = new Set<string>();
  readonly #snapshotOnlyKeys = new Set<string>();
  /** The last frame {@link logOutbound} queued, settled either way. */
  #outbound: Promise<void> = Promise.resolve();

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
      instance: this.#instance,
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
    this.#snapshotOnlyKeys.clear();
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
  logOutbound(message: WorkerMessage): Promise<void> {
    // One frame at a time, in the order they were handed over. The traffic
    // paths call this concurrently — the poll loop, typed input's echo, the
    // link's replies — and the reference handles its frames strictly one
    // after another. Interleaved, a stream chunk awaiting its annotation write
    // would clear the pending key a snapshot recorded in the meantime, and the
    // next chunk would record that match a second time. A frame that fails
    // fails its own caller and does not stop the frames queued behind it.
    const run = this.#outbound.then(() => this.#logOutboundNow(message));
    this.#outbound = run.catch(() => undefined);
    return run;
  }

  /** {@link logOutbound}, once every frame handed over before it is done. */
  async #logOutboundNow(message: WorkerMessage): Promise<void> {
    const type = message.type;
    // One reading of the data, for the wire entry and the scan alike.
    const data = String(message.data ?? "");
    const logger = this.#logger;
    if (logger !== undefined) {
      const payload = type === "term" ? encodeTerminalData(data) : encodeControlFrame(message);
      await logger.logWire("send", payload);
      // term frames are the wire text itself, and a snapshot is recorded in
      // full as a "read" event by #logSnapshot below, as the reference's
      // _log_wire_send skips both.
      if (type !== "term" && type !== "snapshot") {
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
      // The snapshot path DEDUPES (the rule is on the key sets): a match the
      // stream or an earlier snapshot already recorded is skipped; a new one
      // is recorded and left pending for the next stream chunk.
      const key = readAnnotationKey(annotation);
      // An empty key identifies nothing — an annotation the detector did not
      // key — so it is never deduped: recorded, and kept out of both sets.
      if (key !== "") {
        if (this.#readAnnotationKeys.has(key)) {
          continue;
        }
        addBounded(this.#readAnnotationKeys, key);
        addBounded(this.#snapshotOnlyKeys, key);
      }
      await logger.logEvent("annotation", annotationToWire(annotation));
    }
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
   * The stream path records every match — a command run twice is annotated
   * twice — EXCEPT a match in the first chunk after a snapshot that already
   * recorded it (a key in `#snapshotOnlyKeys`), which it consumes once
   * instead; the pending keys are then cleared, matched or not. Every match it
   * records is remembered so the snapshot path, which sees the same text
   * again, does not record it a second time. The full rule is on the key sets.
   */
  async #scanOutput(data: string): Promise<void> {
    const logger = this.#logger;
    const stream = this.#readStream;
    // An empty frame is not a chunk: it leaves the carry and the pending
    // snapshot keys exactly as they were, as the reference's early return does.
    if (logger === undefined || stream === undefined || data === "") {
      return;
    }
    const [text, carry] = splitIncompleteEscape(this.#escapeCarry + data);
    this.#escapeCarry = carry;
    for (const annotation of stream.detect("read", stripAnsi(text), this.#eventSeq)) {
      const key = readAnnotationKey(annotation);
      // An empty key is never deduped, and never remembered (see the snapshot
      // path): it cannot be in the pending set, so it is always recorded.
      if (key !== "") {
        if (this.#snapshotOnlyKeys.delete(key)) {
          continue;
        }
        addBounded(this.#readAnnotationKeys, key);
      }
      await logger.logEvent("annotation", annotationToWire(annotation));
    }
    // Pending snapshot keys expire with the first chunk after the snapshot,
    // matched or not, so they never suppress a later, new match.
    this.#snapshotOnlyKeys.clear();
  }

  /** Record each annotation, in the reference's `to_dict` shape. */
  async #annotate(logger: SessionLogger, annotations: readonly Annotation[] | undefined): Promise<void> {
    for (const annotation of annotations ?? []) {
      await logger.logEvent("annotation", annotationToWire(annotation));
    }
  }
}
