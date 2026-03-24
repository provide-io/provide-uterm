//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// ── Constants ─────────────────────────────────────────────────────────────────
export const _RECONNECT_ANIM_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
export const _DLE = "\x10";
export const _STX = "\x02";
const _CONTROL_LEN_RE = /^[0-9a-fA-F]{8}$/;
// ── Encode helpers ────────────────────────────────────────────────────────────
export function encodeDataFrame(data) {
    return String(data ?? "")
        .split(_DLE)
        .join(_DLE + _DLE);
}
export function encodeControlFrame(payload) {
    const json = JSON.stringify(payload);
    return `${_DLE}${_STX}${json.length.toString(16).padStart(8, "0")}:${json}`;
}
export function encodeWsFrame(payload) {
    const frameType = payload.type;
    if (frameType === "input" || frameType === "term") {
        return encodeDataFrame(payload.data ?? "");
    }
    return encodeControlFrame(payload);
}
// ── Control stream decoder ────────────────────────────────────────────────────
export class ControlStreamDecoder {
    constructor(maxControlBytes = 1024 * 1024) {
        this._buffer = "";
        this._maxControlBytes = maxControlBytes;
    }
    reset() {
        this._buffer = "";
    }
    feed(chunk) {
        this._buffer += String(chunk ?? "");
        const frames = [];
        let cursor = 0;
        let text = "";
        while (cursor < this._buffer.length) {
            const ch = this._buffer[cursor];
            if (ch !== _DLE) {
                text += ch;
                cursor += 1;
                continue;
            }
            if (cursor + 1 >= this._buffer.length) {
                break;
            }
            const marker = this._buffer[cursor + 1];
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
            let parsed;
            try {
                parsed = JSON.parse(this._buffer.slice(payloadStart, payloadEnd));
            }
            catch {
                throw new Error("invalid control payload");
            }
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("control payload must be an object");
            }
            frames.push({ type: "control", control: parsed });
            cursor = payloadEnd;
        }
        if (cursor === this._buffer.length) {
            if (text) {
                frames.push({ type: "data", data: text });
            }
            this._buffer = "";
        }
        else {
            this._buffer = text + this._buffer.slice(cursor);
        }
        return frames;
    }
}
