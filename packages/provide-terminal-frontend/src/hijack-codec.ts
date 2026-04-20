//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/** Types and codec for the inline control stream framing used by ProvideHijack. */

// ── Public types ──────────────────────────────────────────────────────────────

export interface HijackConfig {
  wsUrl?: string;
  workerId?: string;
  wsPathPrefix?: string;
  authToken?: string;
  title?: string | null;
  showInput?: boolean;
  showAnalysis?: boolean;
  heartbeatInterval?: number;
  mobileKeys?: boolean;
  role?: string;
}

/** Resolved config after defaults are merged in. */
export interface ResolvedConfig {
  wsUrl: string | undefined;
  workerId: string | undefined;
  wsPathPrefix: string;
  authToken: string | undefined;
  title: string | null | undefined;
  showInput: boolean;
  showAnalysis: boolean;
  heartbeatInterval: number;
  mobileKeys: boolean;
  role: string | undefined;
}

export type HijackAction = "acquire" | "heartbeat" | "release" | "step";

export interface StreamDataFrame {
  type: "data";
  data: string;
}

export interface StreamControlFrame {
  type: "control";
  control: Record<string, unknown>;
}

export type StreamFrame = StreamDataFrame | StreamControlFrame;

/** Minimal interface for xterm.js Terminal (loaded via CDN). */
export interface XTerminal {
  write(data: string): void;
  reset(): void;
  dispose(): void;
  open(el: HTMLElement): void;
  focus(): void;
  onData(callback: (data: string) => void): { dispose(): void };
  loadAddon(addon: FitAddonInstance): void;
}

export interface FitAddonInstance {
  fit(): void;
}

// ── Constants ─────────────────────────────────────────────────────────────────

export const _RECONNECT_ANIM_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
export const _DLE = "\x10";
export const _STX = "\x02";
const _CONTROL_LEN_RE = /^[0-9a-fA-F]{8}$/;

// ── Encode helpers ────────────────────────────────────────────────────────────

export function encodeDataFrame(data: unknown): string {
  return String(data ?? "")
    .split(_DLE)
    .join(_DLE + _DLE);
}

export function encodeControlFrame(payload: Record<string, unknown>): string {
  const json = JSON.stringify(payload);
  return `${_DLE}${_STX}${json.length.toString(16).padStart(8, "0")}:${json}`;
}

export function encodeWsFrame(payload: Record<string, unknown>): string {
  const frameType = payload.type;
  if (frameType === "input" || frameType === "term") {
    return encodeDataFrame(payload.data ?? "");
  }
  return encodeControlFrame(payload);
}

// ── Control stream decoder ────────────────────────────────────────────────────

export class ControlStreamDecoder {
  private _buffer = "";
  private readonly _maxControlBytes: number;

  constructor(maxControlBytes = 1024 * 1024) {
    this._maxControlBytes = maxControlBytes;
  }

  reset(): void {
    this._buffer = "";
  }

  feed(chunk: string): StreamFrame[] {
    this._buffer += String(chunk ?? "");
    const frames: StreamFrame[] = [];
    let cursor = 0;
    let text = "";

    while (cursor < this._buffer.length) {
      const ch = this._buffer[cursor] as string;
      if (ch !== _DLE) {
        text += ch;
        cursor += 1;
        continue;
      }
      if (cursor + 1 >= this._buffer.length) {
        break;
      }
      const marker = this._buffer[cursor + 1] as string;
      if (marker === _DLE) {
        text += _DLE;
        cursor += 2;
        continue;
      }
      if (marker !== _STX) {
        throw new Error("invalid control stream prefix");
      }
      if (text) {
        frames.push({ type: "data", data: text });
        text = "";
      }
      if (cursor + 11 > this._buffer.length) {
        break;
      }
      const header = this._buffer.slice(cursor + 2, cursor + 10);
      if (!_CONTROL_LEN_RE.test(header)) {
        throw new Error("invalid control stream length");
      }
      if (this._buffer[cursor + 10] !== ":") {
        throw new Error("invalid control stream separator");
      }
      const payloadLength = Number.parseInt(header, 16);
      if (!Number.isFinite(payloadLength) || payloadLength > this._maxControlBytes) {
        throw new Error("control payload too large");
      }
      const payloadStart = cursor + 11;
      const payloadEnd = payloadStart + payloadLength;
      if (payloadEnd > this._buffer.length) {
        break;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(this._buffer.slice(payloadStart, payloadEnd)) as unknown;
      } catch {
        throw new Error("invalid control payload");
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("control payload must be an object");
      }
      frames.push({ type: "control", control: parsed as Record<string, unknown> });
      cursor = payloadEnd;
    }

    if (cursor === this._buffer.length) {
      if (text) {
        frames.push({ type: "data", data: text });
      }
      this._buffer = "";
    } else {
      this._buffer = text + this._buffer.slice(cursor);
    }
    return frames;
  }
}
