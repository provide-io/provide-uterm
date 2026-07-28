//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Generic typed-channel negotiation over the inline control channel.
 *
 * Port of the Python module `provide.uterm.channels` and the Go package
 * `channels`.
 */

import type { ControlFrameChunk } from "../control-channel/index.ts";
import { ControlFrameDecoder, isControlFrame } from "../control-channel/index.ts";

/** Client-advertised typed-channel versions. */
export interface ChannelHello {
  readonly channels: Record<string, number>;
}

/** Fields the `hello_ack` payload owns and callers may not supply. */
const RESERVED_ACK_FIELDS = ["channels", "type"] as const;

/** Construction options for {@link NegotiatedChannels}. */
export interface NegotiatedChannelsOptions {
  /** Channel used when a call does not name one. */
  defaultChannel?: string;
}

/**
 * Coerce a channel map, rejecting anything the reference would reject.
 *
 * JavaScript needs one guard CPython does not: an array is an object here,
 * so it has to be refused explicitly to match `isinstance(value, Mapping)`.
 * Booleans are refused for the same reason CPython refuses them — `True` is
 * an `int` in Python, and a boolean version is a caller mistake either way.
 */
function coerceChannelMap(value: unknown): Record<string, number> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("channels must be a mapping");
  }
  const channels: Record<string, number> = {};
  for (const [name, version] of Object.entries(value)) {
    if (name === "") {
      throw new Error("channel names must be non-empty strings");
    }
    if (typeof version !== "number" || !Number.isInteger(version)) {
      throw new Error("channel versions must be integers");
    }
    channels[name] = version;
  }
  return channels;
}

/** Grant the lower of each requested and supported version, dropping the rest. */
function negotiate(
  supported: Readonly<Record<string, number>>,
  requested: Readonly<Record<string, number>>,
): Record<string, number> {
  const granted: Record<string, number> = {};
  for (const [name, version] of Object.entries(requested)) {
    const supportedVersion = supported[name];
    if (supportedVersion !== undefined && version > 0) {
      granted[name] = Math.min(version, supportedVersion);
    }
  }
  return granted;
}

/** Per-connection typed-channel grants and sequence counters. */
export class NegotiatedChannels {
  readonly #supported: Record<string, number>;
  readonly #defaultChannel: string | undefined;
  #granted: Record<string, number> = {};
  #seq = new Map<string, number>();

  constructor(supported: Record<string, number>, options: NegotiatedChannelsOptions = {}) {
    this.#supported = coerceChannelMap(supported);
    if (Object.keys(this.#supported).length === 0) {
      throw new Error("at least one supported channel is required");
    }
    const defaultChannel = options.defaultChannel;
    if (defaultChannel !== undefined && !(defaultChannel in this.#supported)) {
      throw new Error(`default channel is not supported: ${JSON.stringify(defaultChannel)}`);
    }
    this.#defaultChannel = defaultChannel;
  }

  /** A copy of the currently granted channels. */
  get granted(): Record<string, number> {
    return { ...this.#granted };
  }

  /** Resolve `channel`, falling back to the configured default. */
  #selectChannel(channel: string | undefined): string {
    const selected = channel ?? this.#defaultChannel;
    if (selected === undefined) {
      throw new Error("channel is required when no default_channel is configured");
    }
    return selected;
  }

  /** Whether `channel` is negotiated, defaulting to the primary channel. */
  isNegotiated(channel?: string): boolean {
    return this.#selectChannel(channel) in this.#granted;
  }

  /** Negotiate channel versions and build the `hello_ack` payload. */
  handleHello(hello: unknown, ackFields: Record<string, unknown> = {}): Record<string, unknown> {
    const reserved = RESERVED_ACK_FIELDS.filter((field) => field in ackFields);
    if (reserved.length > 0) {
      throw new Error(`reserved hello_ack field: ${reserved[0]}`);
    }
    const payload = hello as { channels?: unknown };
    this.#granted = negotiate(this.#supported, coerceChannelMap(payload.channels));
    return { type: "hello_ack", channels: { ...this.#granted }, ...ackFields };
  }

  /** Increment and return the sequence number for `channel`. */
  nextSeq(channel?: string): number {
    const selected = this.#selectChannel(channel);
    const next = (this.#seq.get(selected) ?? 0) + 1;
    this.#seq.set(selected, next);
    return next;
  }

  /** A serialisable granted-channel map. */
  exportGrants(): Record<string, number> {
    return { ...this.#granted };
  }

  /**
   * Restore persisted grants, re-negotiated against what this instance
   * supports, and reset the sequence counters for the fresh connection.
   */
  restoreGrants(grants: Record<string, number>): void {
    this.#granted = negotiate(this.#supported, coerceChannelMap(grants));
    this.#seq = new Map();
  }
}

/**
 * Parse a framed `hello` payload, or return `null` when it is not a channel
 * hello or cannot be read as one.
 */
export function parseChannelHello(raw: string): ChannelHello | null {
  if (raw === "" || !isControlFrame(raw)) {
    return null;
  }
  const decoder = new ControlFrameDecoder();
  let chunks: ControlFrameChunk[];
  try {
    chunks = [...decoder.feed(raw), ...decoder.finish()];
  } catch {
    return null;
  }
  for (const chunk of chunks) {
    if (chunk.kind === "control" && chunk.control.type === "hello") {
      try {
        return { channels: coerceChannelMap(chunk.control.channels) };
      } catch {
        return null;
      }
    }
  }
  return null;
}
