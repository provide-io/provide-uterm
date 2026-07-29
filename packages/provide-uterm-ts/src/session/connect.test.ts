//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  connectTelnet,
  connectWs,
  type SessionTransport,
  type TelnetConnectArgs,
  TelnetSession,
  WebSocketSession,
  WS_CLOSE_TIMEOUT_S,
  WS_PING_INTERVAL_S,
  WS_PING_TIMEOUT_S,
  type WsConnectArgs,
} from "./index.ts";

interface RecordedCall {
  args: unknown[];
  kwargs: Record<string, unknown>;
}

interface TelnetCase {
  name: string;
  kwargs: Record<string, unknown>;
  connected_with: RecordedCall;
  sent: string[];
  host: string;
  port: number;
  closes: number;
}

interface WsCase {
  name: string;
  kwargs: Record<string, unknown>;
  construction: RecordedCall;
  connected_with: RecordedCall;
  sent: string[];
  url: string;
  closes: number;
}

interface ConnectGolden {
  telnet: TelnetCase[];
  websocket: WsCase[];
}

const golden = loadGolden<ConnectGolden>("connectsession_golden.json");

/** A transport that records what it was sent and never answers. */
class RecordingTransport implements SessionTransport {
  readonly sent: string[] = [];
  closes = 0;
  #open = true;

  async connect(): Promise<void> {
    return undefined;
  }

  async close(): Promise<void> {
    this.closes += 1;
    this.#open = false;
  }

  async send(data: string): Promise<void> {
    this.sent.push(data);
  }

  async receive(): Promise<string | undefined> {
    // A session with nothing to say: the reader waits rather than ending.
    while (this.#open) {
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    return undefined;
  }
}

/** What a WebSocket transport puts on the wire for this text. */
function utf8Wire(text: string): string {
  return [...new TextEncoder().encode(text)].map((byte) => String.fromCharCode(byte)).join("");
}

/** The settings a recorded case names, in this port's spelling. */
function telnetOptions(kwargs: Record<string, unknown>): Record<string, unknown> {
  const options: Record<string, unknown> = {};
  if (kwargs.cols !== undefined) {
    options.cols = kwargs.cols;
  }
  if (kwargs.rows !== undefined) {
    options.rows = kwargs.rows;
  }
  if (kwargs.term !== undefined) {
    options.term = kwargs.term;
  }
  if (kwargs.connect_timeout !== undefined) {
    options.connectTimeoutS = kwargs.connect_timeout;
  }
  if (kwargs.receive_encoding !== undefined) {
    options.receiveEncoding = kwargs.receive_encoding;
  }
  if (kwargs.control_frames !== undefined) {
    options.controlFrames = kwargs.control_frames;
  }
  return options;
}

function wsOptions(kwargs: Record<string, unknown>): Record<string, unknown> {
  const options: Record<string, unknown> = {};
  const named: Record<string, string> = {
    cols: "cols",
    rows: "rows",
    origin: "origin",
    additional_headers: "additionalHeaders",
    ping_interval: "pingInterval",
    ping_timeout: "pingTimeout",
    close_timeout: "closeTimeout",
    text_frame_encoding: "textFrameEncoding",
    control_frames: "controlFrames",
  };
  for (const [from, to] of Object.entries(named)) {
    if (kwargs[from] !== undefined) {
      options[to] = kwargs[from];
    }
  }
  return options;
}

describe("opening a telnet session", () => {
  it.each(golden.telnet)("$name", async (record) => {
    let asked: TelnetConnectArgs | undefined;
    const transport = new RecordingTransport();
    const session = await connectTelnet("bbs.example", 2323, {
      ...telnetOptions(record.kwargs),
      open: async (args) => {
        asked = args;
        return transport;
      },
    });
    const recorded = record.connected_with.kwargs;
    expect(asked).toEqual({
      host: record.connected_with.args[0],
      port: record.connected_with.args[1],
      cols: recorded.cols,
      rows: recorded.rows,
      term: recorded.term,
      timeout: recorded.timeout,
    });
    expect(session.host).toBe(record.host);
    expect(session.port).toBe(record.port);
    await session.send("hé\r");
    expect(transport.sent).toEqual(record.sent);
    await session.close();
    expect(transport.closes).toBe(record.closes);
  });

  it("advertises the screen it actually has", async () => {
    // Over NAWS, so the far end wraps where this session wraps rather than at
    // whatever it assumed.
    let asked: TelnetConnectArgs | undefined;
    await connectTelnet("bbs.example", 2323, {
      cols: 132,
      rows: 43,
      open: async (args) => {
        asked = args;
        return new RecordingTransport();
      },
    });
    expect(asked).toMatchObject({ cols: 132, rows: 43 });
  });

  it("renders the screen it advertised", async () => {
    // A session that tells the far end 132 columns and then wraps at 80 makes
    // every line the server drew look broken.
    const session = await connectTelnet("bbs.example", 2323, {
      cols: 132,
      rows: 43,
      open: async () => new RecordingTransport(),
    });
    expect(session.snapshot()).toMatchObject({ cols: 132, rows: 43 });
    await session.close();
  });

  it("defaults to the shape a BBS expects", async () => {
    let asked: TelnetConnectArgs | undefined;
    await connectTelnet("bbs.example", 2323, {
      open: async (args) => {
        asked = args;
        return new RecordingTransport();
      },
    });
    expect(asked).toEqual({ host: "bbs.example", port: 2323, cols: 80, rows: 25, term: "ANSI", timeout: 30 });
  });

  it("sends what a BBS reads, not what a browser would", async () => {
    // CP437 on the wire: `é` is one byte, and sending it as UTF-8 would put
    // two of the wrong ones on the line.
    const transport = new RecordingTransport();
    const session = await connectTelnet("bbs.example", 2323, { open: async () => transport });
    await session.send("hé\r");
    expect(transport.sent).toEqual(["h\r"]);
  });

  it("returns a session already connected", async () => {
    const session = await connectTelnet("bbs.example", 2323, { open: async () => new RecordingTransport() });
    expect(session).toBeInstanceOf(TelnetSession);
    expect(session.isConnected()).toBe(true);
    await session.close();
  });
});

describe("opening a WebSocket session", () => {
  it.each(golden.websocket)("$name", async (record) => {
    let asked: WsConnectArgs | undefined;
    const transport = new RecordingTransport();
    const session = await connectWs("wss://feed.example/s", {
      ...wsOptions(record.kwargs),
      open: async (args) => {
        asked = args;
        return transport;
      },
    });
    const recorded = record.connected_with.kwargs;
    expect(asked).toEqual({
      url: recorded.url,
      pingInterval: recorded.ping_interval,
      pingTimeout: recorded.ping_timeout,
      closeTimeout: recorded.close_timeout,
      ...(recorded.origin === undefined ? {} : { origin: recorded.origin }),
      ...(recorded.additional_headers === undefined ? {} : { additionalHeaders: recorded.additional_headers }),
    });
    // Absence is the point, so it is checked by name and not by value.
    expect(Object.keys(asked as object).sort()).toEqual(
      Object.keys(recorded)
        .filter((key) => key !== "host" && key !== "port")
        .map(
          (key) =>
            ({
              url: "url",
              ping_interval: "pingInterval",
              ping_timeout: "pingTimeout",
              close_timeout: "closeTimeout",
              origin: "origin",
              additional_headers: "additionalHeaders",
            })[key],
        )
        .sort(),
    );
    expect(session.url).toBe(record.url);
    await session.send("hé\r");
    // A WebSocket session hands its transport the text and the transport puts
    // UTF-8 on the wire, where a telnet session encodes at the session
    // boundary because CP437 is not something a transport would guess. The
    // corpus records wire bytes either way, so the comparison is made there.
    expect(transport.sent.map(utf8Wire)).toEqual(record.sent);
    await session.close();
    expect(transport.closes).toBe(record.closes);
  });

  it("leaves out an origin nobody gave", async () => {
    // Sending it as nothing is not the same as not sending it: a worker
    // gating cross-origin upgrades refuses a null Origin.
    let asked: WsConnectArgs | undefined;
    await connectWs("wss://feed.example/s", {
      open: async (args) => {
        asked = args;
        return new RecordingTransport();
      },
    });
    expect(Object.keys(asked as object).sort()).toEqual(["closeTimeout", "pingInterval", "pingTimeout", "url"]);
    expect("origin" in (asked as object)).toBe(false);
    expect("additionalHeaders" in (asked as object)).toBe(false);
  });

  it("passes an origin and headers that were given", async () => {
    let asked: WsConnectArgs | undefined;
    await connectWs("wss://feed.example/s", {
      origin: "https://app.example",
      additionalHeaders: { "X-Trace": "1" },
      open: async (args) => {
        asked = args;
        return new RecordingTransport();
      },
    });
    expect(asked?.origin).toBe("https://app.example");
    expect(asked?.additionalHeaders).toEqual({ "X-Trace": "1" });
  });

  it("waits the reference's default number of seconds", async () => {
    let asked: WsConnectArgs | undefined;
    await connectWs("wss://feed.example/s", {
      open: async (args) => {
        asked = args;
        return new RecordingTransport();
      },
    });
    expect(asked).toMatchObject({
      pingInterval: WS_PING_INTERVAL_S,
      pingTimeout: WS_PING_TIMEOUT_S,
      closeTimeout: WS_CLOSE_TIMEOUT_S,
    });
    const recorded = golden.websocket[0]?.connected_with.kwargs;
    expect([WS_PING_INTERVAL_S, WS_PING_TIMEOUT_S, WS_CLOSE_TIMEOUT_S]).toEqual([
      recorded?.ping_interval,
      recorded?.ping_timeout,
      recorded?.close_timeout,
    ]);
  });

  it("sends UTF-8, unlike its telnet sibling", async () => {
    // A WebSocket endpoint is a modern one; the codepage belongs on a
    // telnet line.
    const transport = new RecordingTransport();
    const session = await connectWs("wss://feed.example/s", { open: async () => transport });
    await session.send("hé\r");
    expect(transport.sent).toEqual(["hé\r"]);
    expect(utf8Wire(transport.sent[0] as string)).toBe("h\u00c3\u00a9\r");
  });

  it("returns a session already connected", async () => {
    const session = await connectWs("wss://feed.example/s", { open: async () => new RecordingTransport() });
    expect(session).toBeInstanceOf(WebSocketSession);
    expect(session.isConnected()).toBe(true);
    await session.close();
  });

  it("takes a screen and a codec of its own", async () => {
    const session = await connectWs("wss://feed.example/s", {
      cols: 132,
      rows: 43,
      textFrameEncoding: "cp437",
      open: async () => new RecordingTransport(),
    });
    expect(session.snapshot().cols).toBe(132);
    await session.close();
  });
});

describe("sending and waiting in one step", () => {
  it("reads the change counter before it writes", async () => {
    // A caller that sends first and waits afterwards can miss the answer
    // entirely.
    const transport = new RecordingTransport();
    const session = await connectTelnet("bbs.example", 2323, { open: async () => transport });
    const result = await session.sendExpect("", { expectText: "nothing", timeoutMs: 5 });
    expect(result.matched).toBe(false);
    expect(result.timedOut).toBe(true);
    await session.close();
  });

  it("sends what it was given", async () => {
    const transport = new RecordingTransport();
    const session = await connectTelnet("bbs.example", 2323, { open: async () => transport });
    await session.sendExpect("ls\r", { timeoutMs: 5 });
    expect(transport.sent).toHaveLength(1);
    await session.close();
  });
});
