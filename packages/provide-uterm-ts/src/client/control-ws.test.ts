//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  encodeLogicalFrame,
  type InlineSocket,
  InlineWebSocketClient,
  LogicalFrameDecoder,
  type WsRole,
} from "./index.ts";

interface ControlWsGolden {
  encoded: Array<{ name: string; payload: Record<string, unknown>; encoded: string }>;
  decoded: Array<{ name: string; role: string; chunks: string[]; frames: Array<Record<string, unknown>> }>;
  sent: Array<{
    name: string;
    value: unknown;
    is_bytes: boolean;
    sent: unknown[];
    error: string | null;
    message?: string;
  }>;
  sent_json: Array<{ name: string; value: unknown; sent: unknown[]; error: string | null; message?: string }>;
  received: Array<{
    name: string;
    role: string;
    chunks: string[];
    frames?: Array<Record<string, unknown>>;
    error?: string | null;
    message?: string;
  }>;
  roles: { browser_data_type: string; worker_data_type: string };
}

const golden = loadGolden<ControlWsGolden>("controlws_golden.json");

/** A socket that writes down what it was handed and reads from a script. */
function recorder(chunks: string[] = []): InlineSocket & { sent: unknown[] } {
  const pending = [...chunks];
  return {
    sent: [] as unknown[],
    async send(data) {
      (this as { sent: unknown[] }).sent.push(data);
    },
    async recv() {
      if (pending.length === 0) {
        throw new Error("read past the end of the script");
      }
      return pending.shift() as string;
    },
  };
}

describe("encoding a logical frame", () => {
  it.each(golden.encoded)("$name", (record) => {
    expect(encodeLogicalFrame(record.payload)).toBe(record.encoded);
  });

  it("sends keystrokes as terminal bytes and everything else as a frame", () => {
    // The whole distinction: a control frame typed into a shell would run;
    // terminal bytes read as a frame would be acted on.
    expect(encodeLogicalFrame({ type: "input", data: "ls" })).toBe("ls");
    expect(encodeLogicalFrame({ type: "term", data: "ls" })).toBe("ls");
    expect(encodeLogicalFrame({ type: "hijack_request" })).toContain("hijack_request");
  });

  it("matches the type exactly", () => {
    // A frame typed `INPUT` is not keystrokes, and encoding it as such would
    // type its own JSON into the session.
    for (const type of ["INPUT", "Input", "inputs", " input", "term "]) {
      expect(encodeLogicalFrame({ type, data: "ls" })).toContain(type);
    }
  });

  it("treats a frame with no type as something to act on", () => {
    // Rather than as keystrokes, which is the direction that runs commands.
    expect(encodeLogicalFrame({ data: "ls" })).toContain('"data":"ls"');
    expect(encodeLogicalFrame({})).toContain("{}");
  });

  it("escapes a delimiter in what somebody typed", () => {
    // Otherwise the far end would read the rest as a control payload — a way
    // to type into the control channel.
    expect(encodeLogicalFrame({ type: "input", data: "{}" })).toBe("{}");
  });

  it("sends nothing for keystrokes with nothing in them", () => {
    expect(encodeLogicalFrame({ type: "input" })).toBe("");
  });

  it("stringifies data that is not text, as the reference does", () => {
    expect(encodeLogicalFrame({ type: "input", data: 42 })).toBe("42");
  });
});

describe("decoding a stream back into frames", () => {
  it.each(golden.decoded)("$name, as a $role", (record) => {
    const decoder = new LogicalFrameDecoder(record.role as WsRole);
    const frames = [...record.chunks.flatMap((chunk) => decoder.feed(chunk)), ...decoder.finish()];
    expect(frames).toEqual(record.frames);
  });

  it("names terminal bytes for the direction they are travelling", () => {
    // A worker is being typed at; a browser is being printed to.
    expect(new LogicalFrameDecoder("worker").dataType()).toBe(golden.roles.worker_data_type);
    expect(new LogicalFrameDecoder("browser").dataType()).toBe(golden.roles.browser_data_type);
  });

  it("keeps a frame that arrives in pieces", () => {
    // The decoder is per connection, so a frame split by a read boundary is
    // completed rather than lost.
    const encoded = encodeLogicalFrame({ type: "hello", session_id: "s1" });
    const decoder = new LogicalFrameDecoder("browser");
    expect(decoder.feed(encoded.slice(0, 5))).toEqual([]);
    expect(decoder.feed(encoded.slice(5))).toEqual([{ type: "hello", session_id: "s1" }]);
  });

  it("keeps the two kinds in the order they arrived", () => {
    const decoder = new LogicalFrameDecoder("browser");
    const frames = decoder.feed(
      encodeLogicalFrame({ type: "term", data: "before" }) +
        encodeLogicalFrame({ type: "hijack_state", holder: "ada" }) +
        encodeLogicalFrame({ type: "term", data: "after" }),
    );
    expect(frames).toEqual([
      { type: "term", data: "before" },
      { type: "hijack_state", holder: "ada" },
      { type: "term", data: "after" },
    ]);
  });

  it("refuses a frame the stream ended in the middle of", () => {
    // Silently dropping the tail would turn a protocol break into a frame that
    // simply never arrived — the failure nobody can diagnose.
    const DLE = String.fromCharCode(16);
    for (const truncated of [DLE, `output${DLE}`, `${DLE}${String.fromCharCode(2)}00000005:`]) {
      const decoder = new LogicalFrameDecoder("browser");
      decoder.feed(truncated);
      expect(() => decoder.finish()).toThrow("truncated control frame");
    }
  });

  it("ends quietly when the stream ended between frames", () => {
    const decoder = new LogicalFrameDecoder("browser");
    expect(decoder.feed("output")).toEqual([{ type: "term", data: "output" }]);
    expect(decoder.finish()).toEqual([]);
  });

  it("gives back an escaped delimiter as the byte it was", () => {
    const decoder = new LogicalFrameDecoder("browser");
    expect(decoder.feed(encodeLogicalFrame({ type: "term", data: "{}" }))).toEqual([{ type: "term", data: "{}" }]);
  });
});

describe("what a client will send", () => {
  it.each(golden.sent)("$name", async (record) => {
    const socket = recorder();
    const client = new InlineWebSocketClient(socket, "browser");
    const value = record.is_bytes
      ? Uint8Array.from([...(record.value as string)].map((character) => character.charCodeAt(0)))
      : record.value;
    if (record.error !== null) {
      await expect(client.send(value)).rejects.toThrow(TypeError);
      expect(socket.sent).toEqual([]);
      return;
    }
    await client.send(value);
    const sent = socket.sent.map((entry) => (entry instanceof Uint8Array ? String.fromCharCode(...entry) : entry));
    expect(sent).toEqual(record.sent);
  });

  it("refuses a bare JSON object, which is the one thing that lies", () => {
    // It reads as a control frame and is not one, so sending it loses the
    // frame silently — which is exactly what nobody notices.
    const client = new InlineWebSocketClient(recorder(), "browser");
    return expect(client.send('{"type":"hijack_request"}')).rejects.toThrow(
      "bare JSON control strings are not accepted",
    );
  });

  it("lets through JSON that could not be mistaken for a frame", async () => {
    // A list, a number or a bare string has no type to dispatch on, so it is
    // not the mistake the refusal exists for.
    const socket = recorder();
    const client = new InlineWebSocketClient(socket, "browser");
    for (const value of ["[1,2]", "42", '"hello"', "null", "true"]) {
      await client.send(value);
    }
    expect(socket.sent).toEqual(["[1,2]", "42", '"hello"', "null", "true"]);
  });

  it("lets through text that is not JSON at all", async () => {
    const socket = recorder();
    await new InlineWebSocketClient(socket, "browser").send("just text");
    expect(socket.sent).toEqual(["just text"]);
  });

  it("passes bytes along untouched", async () => {
    // A binary payload is somebody else's protocol.
    const socket = recorder();
    const bytes = Uint8Array.from([1, 2, 3]);
    await new InlineWebSocketClient(socket, "browser").send(bytes);
    expect(socket.sent).toEqual([bytes]);
  });

  it("encodes a mapping as the frame it is", async () => {
    const socket = recorder();
    await new InlineWebSocketClient(socket, "browser").send({ type: "hijack_request" });
    expect(socket.sent).toEqual([encodeLogicalFrame({ type: "hijack_request" })]);
  });

  it("sends keystrokes given as a mapping as terminal bytes", async () => {
    const socket = recorder();
    await new InlineWebSocketClient(socket, "browser").send({ type: "input", data: "ls\n" });
    expect(socket.sent).toEqual(["ls\n"]);
  });
});

describe("what a client will send as JSON", () => {
  it.each(golden.sent_json)("$name", async (record) => {
    const socket = recorder();
    const client = new InlineWebSocketClient(socket, "browser");
    if (record.error !== null) {
      await expect(client.sendJson(record.value)).rejects.toThrow(TypeError);
      expect(socket.sent).toEqual([]);
      return;
    }
    await client.sendJson(record.value);
    expect(socket.sent).toEqual(record.sent);
  });

  it("takes only a mapping", async () => {
    // Everything else has no type to dispatch on.
    const client = new InlineWebSocketClient(recorder(), "browser");
    for (const value of [[1, 2], "hello", 42, null, undefined, true]) {
      await expect(client.sendJson(value)).rejects.toThrow("expected mapping payload");
    }
    await expect(client.sendJson({ type: "hijack_request" })).resolves.toBeUndefined();
  });

  it("names the type it was given the way the reference does", async () => {
    // So a message read in a log means the same thing on both runtimes.
    const client = new InlineWebSocketClient(recorder(), "browser");
    for (const [value, name] of [
      [[1, 2], "list"],
      ["hello", "str"],
      [42, "int"],
      [1.5, "float"],
      [null, "NoneType"],
      [true, "bool"],
    ] as const) {
      await expect(client.sendJson(value)).rejects.toThrow(`got ${name}`);
    }
  });

  it("refuses bytes, which are not a frame either", async () => {
    const client = new InlineWebSocketClient(recorder(), "browser");
    await expect(client.sendJson(Uint8Array.from([1]))).rejects.toThrow("got bytes");
  });
});

describe("what a client reads", () => {
  it.each(golden.received)("$name", async (record) => {
    if (record.error !== undefined && record.error !== null) {
      const client = new InlineWebSocketClient(
        {
          send: async () => {},
          recv: async () => Uint8Array.from([1, 2]),
        },
        "browser",
      );
      await expect(client.recvFrame()).rejects.toThrow(TypeError);
      return;
    }
    const socket = recorder(record.chunks);
    const client = new InlineWebSocketClient(socket, record.role as WsRole);
    const frames = [];
    for (let index = 0; index < (record.frames ?? []).length; index += 1) {
      frames.push(await client.recvFrame());
    }
    expect(frames).toEqual(record.frames);
  });

  it("hands back one frame at a time from a single read", async () => {
    // Reading twice must not read the socket twice when one payload held both.
    const socket = recorder([
      encodeLogicalFrame({ type: "term", data: "out" }) + encodeLogicalFrame({ type: "hijack_state", holder: "ada" }),
    ]);
    const client = new InlineWebSocketClient(socket, "browser");
    expect(await client.recvFrame()).toEqual({ type: "term", data: "out" });
    expect(await client.recvFrame()).toEqual({ type: "hijack_state", holder: "ada" });
  });

  it("keeps reading until there is a whole frame", async () => {
    const encoded = encodeLogicalFrame({ type: "hello", session_id: "s1" });
    const socket = recorder([encoded.slice(0, 4), encoded.slice(4)]);
    const client = new InlineWebSocketClient(socket, "browser");
    expect(await client.recvFrame()).toEqual({ type: "hello", session_id: "s1" });
  });

  it("refuses a payload that is not text", async () => {
    // A binary payload cannot carry this protocol, and reading it as text
    // would corrupt whatever it actually is.
    const client = new InlineWebSocketClient(
      { send: async () => {}, recv: async () => Uint8Array.from([1, 2]) },
      "browser",
    );
    await expect(client.recvFrame()).rejects.toThrow("expected text WebSocket payload, got bytes");
  });

  it("names terminal bytes by the role it was built with", async () => {
    for (const [role, type] of [
      ["browser", "term"],
      ["worker", "input"],
    ] as const) {
      const socket = recorder([encodeLogicalFrame({ type: "term", data: "x" })]);
      expect(await new InlineWebSocketClient(socket, role).recvFrame()).toEqual({ type, data: "x" });
    }
  });
});
