//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { escapeTelnetData, parseTelnetBuffer, replyToDo, replyToWill, TELNET, TelnetBuffer } from "./index.ts";

interface TelnetGolden {
  constants: Record<string, number>;
  parses: Array<{
    name: string;
    bytes: number[];
    streaming: { payload: number[]; events: Array<[string, number, number | number[]]>; consumed: number };
    final: { payload: number[]; events: Array<[string, number, number | number[]]>; consumed: number };
  }>;
  sends: Array<{ name: string; bytes: number[]; escaped: number[] }>;
  do_accepts: number[];
  will_accepts: number[];
}

const golden = loadGolden<TelnetGolden>("telnet_golden.json");

/** Compare a parse result against its recorded shape. */
function expectParse(
  actual: ReturnType<typeof parseTelnetBuffer>,
  expected: TelnetGolden["parses"][number]["streaming"],
): void {
  expect([...actual.payload]).toStrictEqual(expected.payload);
  expect(actual.consumed).toBe(expected.consumed);
  expect(
    actual.events.map((event) =>
      event.kind === "negotiate" ? [event.kind, event.command, event.option] : [event.kind, 0, [...event.payload]],
    ),
  ).toStrictEqual(expected.events);
}

describe("telnet constants", () => {
  it("match the reference", () => {
    expect(TELNET.IAC).toBe(golden.constants.IAC);
    expect(TELNET.WILL).toBe(golden.constants.WILL);
    expect(TELNET.WONT).toBe(golden.constants.WONT);
    expect(TELNET.DO).toBe(golden.constants.DO);
    expect(TELNET.DONT).toBe(golden.constants.DONT);
    expect(TELNET.SB).toBe(golden.constants.SB);
    expect(TELNET.SE).toBe(golden.constants.SE);
    expect(TELNET.OPT_BINARY).toBe(golden.constants.OPT_BINARY);
    expect(TELNET.OPT_ECHO).toBe(golden.constants.OPT_ECHO);
    expect(TELNET.OPT_SGA).toBe(golden.constants.OPT_SGA);
    expect(TELNET.OPT_NAWS).toBe(golden.constants.OPT_NAWS);
    expect(TELNET.OPT_TTYPE).toBe(golden.constants.OPT_TTYPE);
  });
});

describe("parseTelnetBuffer while streaming", () => {
  it.each(golden.parses)("$name", (record) => {
    // The command byte is 0xFF, which a terminal also sends legitimately, so
    // every layer has to agree where a command starts and ends. Disagreeing
    // puts stray command bytes on the operator's screen or swallows content
    // as though it were a negotiation.
    expectParse(parseTelnetBuffer(Uint8Array.from(record.bytes)), record.streaming);
  });

  it("holds back a trailing command byte", () => {
    // The next read may complete it. Treating it as data is the classic
    // split-read bug, and it prints a stray 0xFF.
    const record = golden.parses.find((entry) => entry.name === "trailing command byte");
    expect(record?.streaming.consumed).toBe(1);
    expect(record?.streaming.payload).toStrictEqual([97]);
  });

  it("holds back a truncated negotiation", () => {
    const record = golden.parses.find((entry) => entry.name === "truncated negotiation");
    expect(record?.streaming.consumed).toBe(1);
  });

  it("holds back a subnegotiation that has not ended", () => {
    // Its payload can be any length, so there is no bound at which to give
    // up mid-stream.
    const record = golden.parses.find((entry) => entry.name === "subnegotiation with no end");
    expect(record?.streaming.consumed).toBe(0);
    expect(record?.streaming.payload).toStrictEqual([]);
  });
});

describe("parseTelnetBuffer at the end of a stream", () => {
  it.each(golden.parses)("$name", (record) => {
    expectParse(parseTelnetBuffer(Uint8Array.from(record.bytes), true), record.final);
  });

  it("emits a truncated sequence as literal data", () => {
    // Nothing more is coming, so holding it back would silently drop bytes
    // the server did send.
    for (const name of ["trailing command byte", "truncated negotiation", "truncated subnegotiation"]) {
      const record = golden.parses.find((entry) => entry.name === name);
      expect(record?.final.consumed).toBe(record?.bytes.length);
      expect(record?.final.payload).toStrictEqual(record?.bytes);
    }
  });
});

describe("parseTelnetBuffer sequences", () => {
  it("unescapes a doubled command byte", () => {
    const record = golden.parses.find((entry) => entry.name === "text around an escaped command byte");
    expect(record?.streaming.payload).toStrictEqual([97, 255, 98]);
  });

  it("keeps data either side of a negotiation", () => {
    const record = golden.parses.find((entry) => entry.name === "negotiation between text");
    expect(record?.streaming.payload).toStrictEqual([97, 98]);
  });

  it("reports the subnegotiation payload without its framing", () => {
    const record = golden.parses.find((entry) => entry.name === "subnegotiation");
    expect(record?.streaming.events).toStrictEqual([["subnegotiation", 0, [24, 1]]]);
  });

  it("keeps a command byte that appears inside a subnegotiation payload", () => {
    const record = golden.parses.find((entry) => entry.name === "a command byte inside a subnegotiation payload");
    expect(record?.streaming.events).toStrictEqual([["subnegotiation", 0, [24, 0, 65]]]);
  });

  it("drops a command it does not recognise", () => {
    // Two bytes consumed, nothing emitted: an unknown command is not data,
    // and rendering it would corrupt the screen.
    const record = golden.parses.find((entry) => entry.name === "unknown command is dropped");
    expect(record?.streaming.payload).toStrictEqual([97, 98]);
  });

  it("treats other high bytes as data", () => {
    // Only 0xFF is special; the rest of the high half is screen content.
    const record = golden.parses.find((entry) => entry.name === "high bytes are data");
    expect(record?.streaming.payload).toStrictEqual([128, 200, 254]);
  });
});

describe("escapeTelnetData", () => {
  it.each(golden.sends)("$name", (record) => {
    // Without doubling, a byte the user typed would be read as a command by
    // the far end.
    expect([...escapeTelnetData(Uint8Array.from(record.bytes))]).toStrictEqual(record.escaped);
  });
});

describe("negotiation replies", () => {
  it.each(golden.do_accepts)("accepts DO for option %i", (option) => {
    // Refusing these turns off window-size updates and terminal-type
    // queries, and a BBS then draws for the wrong geometry.
    expect(replyToDo(option).command).toBe(TELNET.WILL);
  });

  it("refuses DO for anything else", () => {
    for (const option of [2, 5, 34, 99]) {
      expect(replyToDo(option).command).toBe(TELNET.WONT);
    }
  });

  it("follows a window-size acceptance with the size itself", () => {
    // Agreeing and then never saying how big the terminal is leaves the
    // server guessing.
    expect(replyToDo(TELNET.OPT_NAWS).thenSend).toBe("naws");
  });

  it("follows a terminal-type acceptance with the type itself", () => {
    expect(replyToDo(TELNET.OPT_TTYPE).thenSend).toBe("ttype");
  });

  it("sends nothing extra for the other accepted options", () => {
    expect(replyToDo(TELNET.OPT_BINARY).thenSend).toBeUndefined();
    expect(replyToDo(TELNET.OPT_SGA).thenSend).toBeUndefined();
  });

  it.each(golden.will_accepts)("accepts WILL for option %i", (option) => {
    expect(replyToWill(option)).toBe(TELNET.DO);
  });

  it("refuses WILL for anything else", () => {
    for (const option of [31, 24, 34, 99]) {
      expect(replyToWill(option)).toBe(TELNET.DONT);
    }
  });
});

describe("TelnetBuffer", () => {
  it("joins a sequence split across reads", () => {
    // The whole reason the parser reports what it consumed: a socket splits
    // wherever it likes, including mid-command.
    const buffer = new TelnetBuffer();
    expect([...buffer.feed(Uint8Array.from([97, TELNET.IAC])).payload]).toStrictEqual([97]);
    const second = buffer.feed(Uint8Array.from([TELNET.DO, TELNET.OPT_NAWS]));
    expect([...second.payload]).toStrictEqual([]);
    expect(second.events).toHaveLength(1);
  });

  it("joins a subnegotiation split across three reads", () => {
    const buffer = new TelnetBuffer();
    buffer.feed(Uint8Array.from([TELNET.IAC, TELNET.SB]));
    buffer.feed(Uint8Array.from([TELNET.OPT_TTYPE, 1]));
    const last = buffer.feed(Uint8Array.from([TELNET.IAC, TELNET.SE]));
    expect(last.events).toStrictEqual([{ kind: "subnegotiation", payload: Uint8Array.from([24, 1]) }]);
  });

  it("flushes what is left when the stream ends", () => {
    const buffer = new TelnetBuffer();
    buffer.feed(Uint8Array.from([97, TELNET.IAC]));
    expect([...buffer.flush().payload]).toStrictEqual([TELNET.IAC]);
  });

  it("holds nothing once everything has been consumed", () => {
    const buffer = new TelnetBuffer();
    buffer.feed(Uint8Array.from([97, 98]));
    expect(buffer.pending).toBe(0);
    buffer.feed(Uint8Array.from([TELNET.IAC]));
    expect(buffer.pending).toBe(1);
  });

  it("flushes to nothing when it is empty", () => {
    expect([...new TelnetBuffer().flush().payload]).toStrictEqual([]);
  });
});
