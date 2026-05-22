//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import { ControlFrameDecoder, decodeControlFrames, encodeControlFrame } from "./controlFrames";

const DLE = "\x10";
const STX = "\x02";

function makeUtf8ControlFrame(payload: Record<string, unknown>): string {
  const json = JSON.stringify(payload);
  const byteLength = new TextEncoder().encode(json).byteLength;
  return `${DLE}${STX}${byteLength.toString(16).padStart(8, "0")}:${json}`;
}

describe("ControlFrameDecoder", () => {
  it("extracts a valid http control frame", () => {
    const frame = { _channel: "http", type: "http_req", id: "req-1", method: "GET" };

    expect(decodeControlFrames(`before${encodeControlFrame(frame)}after`)).toEqual([frame]);
  });

  it("reassembles split frames across feeds", () => {
    const decoder = new ControlFrameDecoder();
    const frame = { _channel: "http", type: "http_res", id: "res-1", status: 200 };
    const encoded = encodeControlFrame(frame);
    const split = Math.floor(encoded.length / 2);

    expect(decoder.feed(encoded.slice(0, split))).toEqual([]);
    expect(decoder.feed(encoded.slice(split))).toEqual([frame]);
  });

  it("lengths encoded non-BMP Unicode payloads in UTF-8 bytes", () => {
    const json = JSON.stringify({ _channel: "http", type: "http_req", text: "👋" });
    const encoded = encodeControlFrame({ _channel: "http", type: "http_req", text: "👋" });
    const length = Number.parseInt(encoded.slice(2, 10), 16);

    expect(length).toBe(new TextEncoder().encode(json).byteLength);
  });

  it("decodes non-BMP Unicode payloads using the UTF-8 byte length header", () => {
    const decoder = new ControlFrameDecoder();
    const frame = { _channel: "http", type: "http_req", text: "👋" };

    expect(decoder.feed(makeUtf8ControlFrame(frame))).toEqual([frame]);
  });

  it("enforces max payload size in UTF-8 bytes", () => {
    const json = JSON.stringify({ text: "👋" });
    const byteLength = new TextEncoder().encode(json).byteLength;
    const frame = `${DLE}${STX}${byteLength.toString(16).padStart(8, "0")}:${json}`;
    const decoder = new ControlFrameDecoder({ maxPayloadBytes: byteLength - 1 });

    expect(() => decoder.feed(frame)).toThrow("control payload too large");
  });

  it("rejects payloads larger than the configured bound", () => {
    const decoder = new ControlFrameDecoder({ maxPayloadBytes: 8 });
    const frame = encodeControlFrame({ _channel: "http", type: "http_req" });

    expect(() => decoder.feed(frame)).toThrow("control payload too large");
  });

  it("rejects non-hex length headers", () => {
    const decoder = new ControlFrameDecoder();

    expect(() => decoder.feed(`${DLE}${STX}zzzzzzzz:{}`)).toThrow("invalid control frame length");
  });

  it("rejects non-object JSON payloads", () => {
    const decoder = new ControlFrameDecoder();
    const payload = JSON.stringify(["not", "an", "object"]);
    const encoded = `${DLE}${STX}${payload.length.toString(16).padStart(8, "0")}:${payload}`;

    expect(() => decoder.feed(encoded)).toThrow("control payload must be an object");
  });

  it("ignores escaped DLE data around control frames", () => {
    const decoder = new ControlFrameDecoder();
    const frame = { _channel: "http", type: "http_intercept_state", enabled: true };

    expect(decoder.feed(`left${DLE}${DLE}${encodeControlFrame(frame)}${DLE}${DLE}right`)).toEqual([
      frame,
    ]);
  });
});
