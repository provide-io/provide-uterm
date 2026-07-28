//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  decodeSessionText,
  encodeSessionText,
  type SessionTransport,
  TELNET_SESSION_DEFAULTS,
  TelnetSession,
  WEBSOCKET_SESSION_DEFAULTS,
  WebSocketSession,
} from "./index.ts";

interface AdaptersGolden {
  telnet_defaults: Record<string, unknown>;
  ws_defaults: Record<string, unknown>;
  encodes: Array<{ name: string; text: string; bytes: number[] }>;
  decodes: Array<{ name: string; bytes: number[]; text: string }>;
}

const golden = loadGolden<AdaptersGolden>("session_adapters_golden.json");

/** A transport that records what it was asked to do. */
class RecordingTransport implements SessionTransport {
  readonly sent: string[] = [];
  connected = false;
  #resolve: (() => void) | undefined;

  async connect(): Promise<void> {
    this.connected = true;
  }

  async close(): Promise<void> {
    this.connected = false;
    this.#resolve?.();
  }

  async send(data: string): Promise<void> {
    this.sent.push(data);
  }

  async receive(): Promise<string | undefined> {
    await new Promise<void>((resolve) => {
      this.#resolve = resolve;
    });
    return undefined;
  }
}

describe("session encodings", () => {
  it.each(golden.encodes)("encodes $name as CP437", (record) => {
    // Not cosmetic: a BBS expects one CP437 byte where UTF-8 would put two,
    // and the screen desynchronises from that point on.
    expect([...encodeSessionText(record.text, "cp437")]).toStrictEqual(record.bytes);
  });

  it.each(golden.decodes)("decodes $name from CP437", (record) => {
    expect(decodeSessionText(Uint8Array.from(record.bytes), "cp437")).toBe(record.text);
  });

  it("maps the high half rather than mangling it", () => {
    // The half that carries every box-drawing and block character a BBS
    // draws its interface with.
    const record = golden.decodes.find((entry) => entry.name === "the whole high half");
    expect(decodeSessionText(Uint8Array.from(record?.bytes ?? []), "cp437")).toBe(record?.text);
    expect(record?.text).not.toContain("�");
  });

  it("substitutes a character CP437 cannot carry", () => {
    // Better a visible placeholder than a thrown error mid-stream.
    const record = golden.encodes.find((entry) => entry.name === "greek in the high range");
    expect(record?.bytes).toContain(63);
  });

  it("passes latin-1 through as bytes", () => {
    // A websocket text frame already carries characters; latin-1 is the
    // identity that turns them back into the bytes they stood for.
    expect([...encodeSessionText("é", "latin-1")]).toStrictEqual([0xe9]);
    expect(decodeSessionText(Uint8Array.from([0xe9]), "latin-1")).toBe("é");
  });

  it("substitutes a character latin-1 cannot carry either", () => {
    // The identity only holds below 0x100. Above it, truncating to the low
    // byte would put a plausible-looking wrong character on the wire, so a
    // question mark is substituted the way CPython does.
    expect([...encodeSessionText("€", "latin-1")]).toStrictEqual([0x3f]);
    expect([...encodeSessionText("a€b", "latin-1")]).toStrictEqual([0x61, 0x3f, 0x62]);
  });
});

describe("TelnetSession", () => {
  it("uses the reference defaults", () => {
    expect(TELNET_SESSION_DEFAULTS.cols).toBe(golden.telnet_defaults.cols);
    expect(TELNET_SESSION_DEFAULTS.rows).toBe(golden.telnet_defaults.rows);
    expect(TELNET_SESSION_DEFAULTS.term).toBe(golden.telnet_defaults.term);
    expect(TELNET_SESSION_DEFAULTS.connectTimeoutS).toBe(golden.telnet_defaults.connect_timeout);
    expect(TELNET_SESSION_DEFAULTS.receiveEncoding).toBe(golden.telnet_defaults.receive_encoding);
    expect(TELNET_SESSION_DEFAULTS.controlFrames).toBe(golden.telnet_defaults.control_frames);
  });

  it("sends as CP437", async () => {
    // The high-byte conventions a BBS expects on the wire.
    const transport = new RecordingTransport();
    const session = new TelnetSession({ transport, host: "bbs.example", port: 23 });
    await session.connect();
    await session.send("café\r");
    expect([...(session.lastSentBytes ?? [])]).toStrictEqual([99, 97, 102, 130, 13]);
    await session.close();
  });

  it("carries its host and port for diagnostics", async () => {
    const transport = new RecordingTransport();
    const session = new TelnetSession({ transport, host: "bbs.example", port: 2102 });
    expect(session.host).toBe("bbs.example");
    expect(session.port).toBe(2102);
  });

  it("leaves control frames off by default", async () => {
    // A plain telnet client shows every byte; opting in is the caller's
    // choice, not the default.
    expect(TELNET_SESSION_DEFAULTS.controlFrames).toBe(false);
  });

  it("decodes what it receives as CP437", async () => {
    const transport = new RecordingTransport();
    const session = new TelnetSession({ transport, host: "h", port: 23 });
    await session.connect();
    expect(session.decode(Uint8Array.from([201, 205, 187]))).toBe("╔═╗");
    await session.close();
  });
});

describe("WebSocketSession", () => {
  it("uses the reference defaults", () => {
    expect(WEBSOCKET_SESSION_DEFAULTS.cols).toBe(golden.ws_defaults.cols);
    expect(WEBSOCKET_SESSION_DEFAULTS.rows).toBe(golden.ws_defaults.rows);
    expect(WEBSOCKET_SESSION_DEFAULTS.pingIntervalS).toBe(golden.ws_defaults.ping_interval);
    expect(WEBSOCKET_SESSION_DEFAULTS.pingTimeoutS).toBe(golden.ws_defaults.ping_timeout);
    expect(WEBSOCKET_SESSION_DEFAULTS.closeTimeoutS).toBe(golden.ws_defaults.close_timeout);
    expect(WEBSOCKET_SESSION_DEFAULTS.receiveEncoding).toBe(golden.ws_defaults.receive_encoding);
    expect(WEBSOCKET_SESSION_DEFAULTS.textFrameEncoding).toBe(golden.ws_defaults.text_frame_encoding);
    expect(WEBSOCKET_SESSION_DEFAULTS.controlFrames).toBe(golden.ws_defaults.control_frames);
  });

  it("decodes terminal bytes as CP437 but a text frame as latin-1", async () => {
    // They are different questions. A binary frame carries bytes a BBS drew;
    // a text frame already carries characters, and latin-1 is the identity
    // that turns them back into the bytes they stood for.
    expect(WEBSOCKET_SESSION_DEFAULTS.receiveEncoding).toBe("cp437");
    expect(WEBSOCKET_SESSION_DEFAULTS.textFrameEncoding).toBe("latin-1");
    const transport = new RecordingTransport();
    const session = new WebSocketSession({ transport, url: "wss://hub.example/ws" });
    await session.connect();
    expect(session.decode(Uint8Array.from([201]))).toBe("╔");
    expect(session.decodeTextFrame("é")).toBe("é");
    await session.close();
  });

  it("carries its url for diagnostics", () => {
    const transport = new RecordingTransport();
    const session = new WebSocketSession({ transport, url: "wss://hub.example/ws" });
    expect(session.url).toBe("wss://hub.example/ws");
  });

  it("keeps the ping settings a quiet session needs", () => {
    // A passive worker emits nothing for minutes; without keepalives the
    // socket is reaped by whatever sits in the middle.
    expect(WEBSOCKET_SESSION_DEFAULTS.pingIntervalS).toBeGreaterThan(0);
    expect(WEBSOCKET_SESSION_DEFAULTS.pingTimeoutS).toBeGreaterThan(0);
  });
});
