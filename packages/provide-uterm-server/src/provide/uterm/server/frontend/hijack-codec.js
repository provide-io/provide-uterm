//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// ── Constants ─────────────────────────────────────────────────────────────────
export const _RECONNECT_ANIM_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
export const _DLE = "\x10";
export const _STX = "\x02";
const _CONTROL_LEN_RE = /^[0-9a-fA-F]{8}$/;
const _TEXT_ENCODER = new TextEncoder();
const _DEFAULT_MAX_CONTROL_BYTES = 1024 * 1024;
const _DEFAULT_MAX_BUFFER_BYTES = 10 * 1024 * 1024;
const _DEFAULT_MAX_CONTROL_DEPTH = 32;
function utf8ByteLength(value) {
    return _TEXT_ENCODER.encode(value).byteLength;
}
function checkJsonDepth(value, maxDepth) {
    const stack = [{ node: value, depth: 1 }];
    while (stack.length > 0) {
        const entry = stack.pop();
        if (entry.depth > maxDepth) {
            throw new Error(`control payload nests deeper than ${maxDepth}`);
        }
        if (Array.isArray(entry.node)) {
            for (const child of entry.node) {
                if (typeof child === "object" && child !== null) {
                    stack.push({ node: child, depth: entry.depth + 1 });
                }
            }
            continue;
        }
        if (typeof entry.node !== "object" || entry.node === null) {
            continue;
        }
        for (const child of Object.values(entry.node)) {
            if (typeof child === "object" && child !== null) {
                stack.push({ node: child, depth: entry.depth + 1 });
            }
        }
    }
}
function utf8PayloadEnd(raw, start, payloadBytes) {
    let byteCount = 0;
    let cursor = start;
    while (cursor < raw.length && byteCount < payloadBytes) {
        const codePoint = raw.codePointAt(cursor);
        if (codePoint === undefined)
            break;
        const char = String.fromCodePoint(codePoint);
        byteCount += utf8ByteLength(char);
        cursor += char.length;
        if (byteCount > payloadBytes) {
            throw new Error("invalid control payload length");
        }
    }
    return byteCount === payloadBytes ? cursor : null;
}
// ── Encode helpers ────────────────────────────────────────────────────────────
export function encodeDataFrame(data) {
    return String(data ?? "")
        .split(_DLE)
        .join(_DLE + _DLE);
}
export function encodeControlFrame(payload) {
    const json = JSON.stringify(payload);
    return `${_DLE}${_STX}${utf8ByteLength(json).toString(16).padStart(8, "0")}:${json}`;
}
export function encodeWsFrame(payload) {
    const frameType = payload.type;
    if (frameType === "input" || frameType === "term") {
        return encodeDataFrame(payload.data ?? "");
    }
    return encodeControlFrame(payload);
}
// ── Control stream decoder ────────────────────────────────────────────────────
export class ControlChannelDecoder {
    constructor(maxControlBytes = _DEFAULT_MAX_CONTROL_BYTES, maxBufferBytes = _DEFAULT_MAX_BUFFER_BYTES, maxFrameDepth = _DEFAULT_MAX_CONTROL_DEPTH) {
        this._buffer = "";
        this._maxControlBytes = maxControlBytes;
        this._maxBufferBytes = maxBufferBytes;
        this._maxFrameDepth = maxFrameDepth;
    }
    reset() {
        this._buffer = "";
    }
    feed(chunk) {
        this._buffer += String(chunk ?? "");
        if (utf8ByteLength(this._buffer) > this._maxBufferBytes) {
            this._buffer = "";
            throw new Error("control channel buffer overflow");
        }
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
                throw new Error("invalid control channel prefix");
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
                throw new Error("invalid control channel length");
            }
            if (this._buffer[cursor + 10] !== ":") {
                throw new Error("invalid control channel separator");
            }
            const payloadLength = Number.parseInt(header, 16);
            if (!Number.isFinite(payloadLength) || payloadLength > this._maxControlBytes) {
                throw new Error("control payload too large");
            }
            const payloadStart = cursor + 11;
            const payloadEnd = utf8PayloadEnd(this._buffer, payloadStart, payloadLength);
            if (payloadEnd === null) {
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
            checkJsonDepth(parsed, this._maxFrameDepth);
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
