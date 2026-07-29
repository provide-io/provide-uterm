//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { deriveColormode, IacNegotiator, MAX_SUBNEGOTIATION_BYTES } from "./index.ts";

interface IacGolden {
  start: string;
  streams: Array<{
    name: string;
    chunked: boolean;
    stream: string;
    start: string;
    cleaned: string;
    reply: string;
    term: string;
    env: Record<string, string>;
    done: boolean;
    colormode: string | null;
  }>;
  colours: Array<{ name: string; term: string | null; env: Record<string, string>; colormode: string | null }>;
}

const golden = loadGolden<IacGolden>("iac_golden.json");

/** The corpus carries arbitrary bytes as latin-1. */
function bytes(text: string): Uint8Array {
  return Uint8Array.from([...text].map((character) => character.charCodeAt(0)));
}

function text(data: Uint8Array): string {
  return String.fromCharCode(...data);
}

const IAC = 255;
const SB = 250;
const SE = 240;
const WILL = 251;
const TTYPE = 24;
const NEW_ENVIRON = 39;
const IS = 0;
const VAR = 0;
const VALUE = 1;

/** A client answering with its terminal type. */
function ttypeIs(name: string): Uint8Array {
  return Uint8Array.from([IAC, SB, TTYPE, IS, ...[...name].map((c) => c.charCodeAt(0)), IAC, SE]);
}

describe("what the gateway asks for", () => {
  it("asks both questions the moment a client connects", () => {
    expect(text(new IacNegotiator().startBytes())).toBe(golden.start);
  });

  it("has heard nothing before a client says anything", () => {
    const negotiator = new IacNegotiator();
    expect(negotiator.term).toBe("");
    expect(negotiator.env).toEqual({});
    expect(negotiator.derivedColormode()).toBeUndefined();
  });

  it("is not finished until both questions are answered", () => {
    const negotiator = new IacNegotiator();
    negotiator.startBytes();
    expect(negotiator.done()).toBe(false);
    negotiator.feed(ttypeIs("xterm"));
    expect(negotiator.done()).toBe(false);
    negotiator.feed(Uint8Array.from([IAC, SB, NEW_ENVIRON, IS, IAC, SE]));
    expect(negotiator.done()).toBe(true);
  });

  it("is finished before it has asked anything", () => {
    // Nothing was asked, so nothing is outstanding.
    expect(new IacNegotiator().done()).toBe(true);
  });
});

describe("reading what a client sends", () => {
  /**
   * The rows where the reference's answer depends on how the stream was cut.
   *
   * Inside a subnegotiation the reference appends a dangling `IAC` to the
   * payload as a literal instead of holding it for the next read, so a
   * terminating `IAC SE` split across two reads is swallowed: the
   * subnegotiation never ends, the terminal type is never learnt, and
   * `done()` never becomes true. A two-byte terminator landing on a segment
   * boundary is ordinary, so this is reachable from a real client.
   *
   * The port holds it, which is why these rows are compared against the
   * whole-stream answer instead.
   */
  const chunkIndependent = new Map(golden.streams.filter((row) => !row.chunked).map((row) => [row.name, row]));

  it.each(golden.streams)("$name, $chunked", (record) => {
    // A stream's meaning is not a function of where the reads fell.
    const expected = record.chunked ? (chunkIndependent.get(record.name) ?? record) : record;
    const negotiator = new IacNegotiator();
    expect(text(negotiator.startBytes())).toBe(record.start);
    const pieces = record.chunked ? [...record.stream].map((character) => bytes(character)) : [bytes(record.stream)];
    let cleaned = "";
    let reply = "";
    for (const piece of pieces) {
      const result = negotiator.feed(piece);
      cleaned += text(result.cleaned);
      reply += text(result.reply);
    }
    expect({
      cleaned,
      reply,
      term: negotiator.term,
      env: negotiator.env,
      done: negotiator.done(),
      colormode: negotiator.derivedColormode() ?? null,
    }).toEqual({
      cleaned: expected.cleaned,
      reply: expected.reply,
      term: expected.term,
      env: expected.env,
      done: expected.done,
      colormode: expected.colormode,
    });
  });

  it("finishes a handshake whose terminator was split across two reads", () => {
    // The reference loses this one: it takes the dangling `IAC` for payload,
    // so the subnegotiation never ends and the client is never identified.
    const whole = ttypeIs("xterm-256color");
    const negotiator = new IacNegotiator();
    negotiator.startBytes();
    negotiator.feed(whole.slice(0, whole.length - 1));
    expect(negotiator.term).toBe("");
    negotiator.feed(whole.slice(whole.length - 1));
    expect(negotiator.term).toBe("xterm-256color");
  });

  it("gives the same answer however the stream was cut", () => {
    // Every split point, for a stream holding both kinds of sequence.
    const whole = Uint8Array.from([104, 105, IAC, WILL, TTYPE, ...ttypeIs("xterm-256color"), IAC, IAC, 33]);
    const once = new IacNegotiator();
    once.startBytes();
    const reference = once.feed(whole);
    for (let split = 1; split < whole.length; split += 1) {
      const split_ = new IacNegotiator();
      split_.startBytes();
      const first = split_.feed(whole.slice(0, split));
      const second = split_.feed(whole.slice(split));
      expect(text(first.cleaned) + text(second.cleaned)).toBe(text(reference.cleaned));
      expect(text(first.reply) + text(second.reply)).toBe(text(once.term === "" ? first.reply : reference.reply));
      expect(split_.term).toBe(once.term);
    }
  });

  it("takes every protocol byte out of what goes upstream", () => {
    // A stray IAC reaching a shell is a byte nobody typed.
    const negotiator = new IacNegotiator();
    const result = negotiator.feed(Uint8Array.from([104, 105, IAC, WILL, TTYPE, 33, ...ttypeIs("xterm"), 63]));
    expect(text(result.cleaned)).toBe("hi!?");
  });

  it("gives back an escaped delimiter as the one byte it stands for", () => {
    expect(text(new IacNegotiator().feed(Uint8Array.from([IAC, IAC])).cleaned)).toBe("\xff");
  });

  it("holds a sequence that arrives in pieces", () => {
    // It is a byte stream: a read boundary can fall anywhere.
    const negotiator = new IacNegotiator();
    const whole = ttypeIs("xterm-256color");
    for (let split = 1; split < whole.length; split += 1) {
      const one = new IacNegotiator();
      one.feed(whole.slice(0, split));
      one.feed(whole.slice(split));
      expect(one.term).toBe("xterm-256color");
    }
    expect(negotiator.term).toBe("");
  });

  it("holds a lone delimiter at the end of a read", () => {
    const negotiator = new IacNegotiator();
    expect(text(negotiator.feed(Uint8Array.from([104, IAC])).cleaned)).toBe("h");
    expect(text(negotiator.feed(Uint8Array.from([IAC])).cleaned)).toBe("\xff");
  });

  it("holds a verb whose option has not arrived", () => {
    const negotiator = new IacNegotiator();
    expect(text(negotiator.feed(Uint8Array.from([IAC, WILL])).reply)).toBe("");
    expect(text(negotiator.feed(Uint8Array.from([TTYPE])).reply)).toContain("\xfa");
  });

  it("asks for what a client agrees to send", () => {
    for (const option of [TTYPE, NEW_ENVIRON]) {
      const reply = new IacNegotiator().feed(Uint8Array.from([IAC, WILL, option])).reply;
      expect([...reply]).toEqual([IAC, SB, option, 1, IAC, SE]);
    }
  });

  it("says nothing to an option nobody asked about", () => {
    // Carried over from the reference. RFC 854 asks for a refusal, and a
    // client waiting for one waits forever; the roadmap records it.
    for (const message of [
      [IAC, WILL, 99],
      [IAC, 253, 99],
      [IAC, 252, TTYPE],
      [IAC, 254, TTYPE],
    ]) {
      expect([...new IacNegotiator().feed(Uint8Array.from(message)).reply]).toEqual([]);
    }
  });

  it("reads the last terminal type a client sends", () => {
    const negotiator = new IacNegotiator();
    negotiator.feed(ttypeIs("xterm"));
    negotiator.feed(ttypeIs("vt100"));
    expect(negotiator.term).toBe("vt100");
  });

  it("reads several environment variables from one message", () => {
    const negotiator = new IacNegotiator();
    negotiator.feed(
      Uint8Array.from([
        IAC,
        SB,
        NEW_ENVIRON,
        IS,
        VAR,
        ...[..."COLORTERM"].map((c) => c.charCodeAt(0)),
        VALUE,
        ...[..."truecolor"].map((c) => c.charCodeAt(0)),
        VAR,
        ...[..."LANG"].map((c) => c.charCodeAt(0)),
        VALUE,
        ...[..."en_GB"].map((c) => c.charCodeAt(0)),
        IAC,
        SE,
      ]),
    );
    expect(negotiator.env).toEqual({ COLORTERM: "truecolor", LANG: "en_GB" });
  });

  it("reads a variable with no value as empty", () => {
    const negotiator = new IacNegotiator();
    negotiator.feed(
      Uint8Array.from([IAC, SB, NEW_ENVIRON, IS, VAR, ...[..."EMPTY"].map((c) => c.charCodeAt(0)), IAC, SE]),
    );
    expect(negotiator.env).toEqual({ EMPTY: "" });
  });

  it("takes a literal byte after an escape inside an environment", () => {
    // So a value holding what would otherwise be a separator survives.
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, NEW_ENVIRON, IS, VAR, 75, VALUE, 2, VAR, 66, IAC, SE]));
    expect(negotiator.env).toEqual({ K: "\x00B" });
  });

  it("drops a control the outer gateway already handles", () => {
    // Interrupt, break, end-of-file and the rest are not application data,
    // and passing them upstream would type them into the session.
    const negotiator = new IacNegotiator();
    const result = negotiator.feed(Uint8Array.from([104, IAC, 244, 105]));
    expect(text(result.cleaned)).toBe("hi");
    expect([...result.reply]).toEqual([]);
  });

  it("ignores a stray delimiter inside a subnegotiation", () => {
    // Neither escaped nor a terminator: not payload, and not something to
    // act on in the middle of one.
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, TTYPE, IS, 120, IAC, 241, 116, IAC, SE]));
    expect(negotiator.term).toBe("xt");
  });

  it("takes an escape at the very end of an environment as nothing", () => {
    // There is no byte left for it to make literal, so there is nothing to
    // add — rather than reading past the end of what arrived.
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, NEW_ENVIRON, IS, VAR, 75, VALUE, 118, 2, IAC, SE]));
    expect(negotiator.env).toEqual({ K: "v" });
  });

  it("invents no variable from a marker with no name", () => {
    // A marker followed straight by a value names nothing, and storing it
    // would put an empty key in the environment the session is opened with.
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, NEW_ENVIRON, IS, VAR, VALUE, 120, IAC, SE]));
    expect(negotiator.env).toEqual({});
  });

  it("invents no variable from bytes before the first marker", () => {
    // Nothing is a variable until a marker says one has started; treating
    // leading bytes as a name would make one up out of padding.
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, NEW_ENVIRON, IS, 106, 117, 110, 107, IAC, SE]));
    expect(negotiator.env).toEqual({});
  });

  it("ignores a subnegotiation for something it does not read", () => {
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, 99, IS, 1, 2, 3, IAC, SE]));
    expect(negotiator.term).toBe("");
    expect(negotiator.env).toEqual({});
  });

  it("ignores a subnegotiation that is not an answer", () => {
    // `SEND` is the gateway's own verb; a client echoing it says nothing.
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, TTYPE, 1, 120, IAC, SE]));
    expect(negotiator.term).toBe("");
  });

  it("ignores a subnegotiation too short to mean anything", () => {
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, TTYPE, IAC, SE]));
    negotiator.feed(Uint8Array.from([IAC, SB, IAC, SE]));
    expect(negotiator.term).toBe("");
  });

  it("bounds what it keeps from a subnegotiation nobody closes", () => {
    // A client that opens one and never ends it costs a fixed amount of
    // memory rather than all of it.
    const negotiator = new IacNegotiator();
    negotiator.feed(Uint8Array.from([IAC, SB, TTYPE, IS]));
    for (let index = 0; index < 100; index += 1) {
      negotiator.feed(Uint8Array.from(new Array(1000).fill(120)));
    }
    negotiator.feed(Uint8Array.from([IAC, SE]));
    // The cap itself, not merely that there is one: a cap large enough to be
    // worth attacking is not a cap.
    expect(MAX_SUBNEGOTIATION_BYTES).toBe(512);
    // Two short of the cap: the option and the kind are the first two bytes
    // of the same buffer, so they come out of the same budget.
    expect(negotiator.term.length).toBe(MAX_SUBNEGOTIATION_BYTES - 2);
  });

  it("carries on with the client's own data after a subnegotiation", () => {
    const negotiator = new IacNegotiator();
    const result = negotiator.feed(Uint8Array.from([...ttypeIs("xterm"), 104, 105]));
    expect(text(result.cleaned)).toBe("hi");
    expect(negotiator.term).toBe("xterm");
  });
});

describe("which colours a session is opened with", () => {
  it.each(golden.colours)("$name", (record) => {
    expect(deriveColormode(record.term ?? undefined, record.env) ?? null).toBe(record.colormode);
  });

  it("lets a true-colour hint win over the terminal's own name", () => {
    // A terminal that says it can do more than its name suggests is believed.
    expect(deriveColormode("vt100", { COLORTERM: "truecolor" })).toBe("passthrough");
    expect(deriveColormode("vt100", {})).toBe("16");
  });

  it("takes either spelling of a true-colour hint, in any case", () => {
    for (const hint of ["truecolor", "24bit", "TRUECOLOR", "24BIT", "TrueColor"]) {
      expect(deriveColormode("vt100", { COLORTERM: hint })).toBe("passthrough");
    }
  });

  it("ignores a hint nobody defined", () => {
    expect(deriveColormode("xterm", { COLORTERM: "sideways" })).toBe("16");
  });

  it("reads 256 colours from the end of a name, not anywhere in it", () => {
    for (const term of ["xterm-256color", "XTERM-256COLOR", "screen-256color"]) {
      expect(deriveColormode(term, {})).toBe("256");
    }
    // A name that merely contains it is a name nobody listed, and answering
    // for it would tell the upstream something that might be wrong.
    for (const term of ["xterm-256color-italic", "256color", "my-256color-thing"]) {
      expect(deriveColormode(term, {})).toBeUndefined();
    }
  });

  it("says nothing at all about a terminal it does not know", () => {
    // Leaving the upstream to decide, rather than telling it something wrong.
    for (const term of ["", undefined, "screen", "sideways", "xterm-color", "xterm2"]) {
      expect(deriveColormode(term, {})).toBeUndefined();
    }
  });

  it("gives a terminal it knows by name the plain sixteen", () => {
    // Named exactly rather than matched by prefix: `xterm-color` is not
    // `xterm`, and guessing for it would be telling the upstream something
    // that might be wrong.
    for (const term of ["xterm", "vt100", "vt102", "vt220", "ansi", "linux", "dumb"]) {
      expect(deriveColormode(term, {})).toBe("16");
    }
  });

  it("reads a direct-colour terminal as true colour", () => {
    for (const term of ["xterm-direct", "screen-truecolor", "XTERM-DIRECT"]) {
      expect(deriveColormode(term, {})).toBe("passthrough");
    }
  });

  it("falls back to the terminal in the environment", () => {
    // A client that answered the environment question but not the terminal
    // one has still said what it is.
    expect(deriveColormode(undefined, { TERM: "xterm-256color" })).toBe("256");
    expect(deriveColormode("", { TERM: "vt100" })).toBe("16");
    // The one it named directly wins.
    expect(deriveColormode("vt100", { TERM: "xterm-256color" })).toBe("16");
  });

  it("ignores the spaces around what it was told", () => {
    expect(deriveColormode("  xterm-256color  ", {})).toBe("256");
    expect(deriveColormode("xterm", { COLORTERM: "  truecolor  " })).toBe("passthrough");
  });

  it("answers from a hint even with no terminal at all", () => {
    expect(deriveColormode(undefined, { COLORTERM: "truecolor" })).toBe("passthrough");
  });
});
