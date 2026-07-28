//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type SshProcess, SshStreamReader, SshStreamWriter } from "./index.ts";

interface SshGolden {
  reads: Array<{ name: string; result: number[] }>;
  writes: {
    live: { written: number[][]; drained: number };
    write_failure: { attempts: number; exited: number[]; closed: number };
    drain_failure: { exited: number[]; closed: number };
    after_close: { written: number[][]; drained: number; exited: number[]; closed: number };
  };
  peers: Array<{ name: string; peer: unknown; result: unknown; other: string }>;
}

const golden = loadGolden<SshGolden>("sshtransport_golden.json");

/** What each corpus read case hands back, in the order the corpus records. */
const READ_VALUES: Record<string, () => unknown> = {
  bytes: () => Buffer.from("hello"),
  "a bytearray": () => Uint8Array.from(Buffer.from("hello")),
  text: () => "hello",
  "text that is not ascii": () => "héllo → ✓",
  "text carrying a lone surrogate": () => "a\udc80b",
  nothing: () => Buffer.alloc(0),
  "an empty string": () => "",
  "something that is neither": () => 7,
  "nothing at all": () => null,
  "a closed connection": () => {
    throw new Error("connection lost");
  },
  "an end of file": () => {
    throw new Error("EOF");
  },
  "a cancelled read": () => {
    throw new Error("cancelled");
  },
};

/** A process whose halves answer as the test asks. */
function makeProcess(options: {
  read?: (() => unknown) | undefined;
  onWrite?: (data: Uint8Array) => void;
  writeThrows?: boolean;
  drainThrows?: boolean;
  peer?: unknown;
}): SshProcess & { exited: number[]; closed: number; drained: number } {
  const state = { exited: [] as number[], closed: 0, drained: 0 };
  return {
    ...state,
    get exited() {
      return state.exited;
    },
    get closed() {
      return state.closed;
    },
    get drained() {
      return state.drained;
    },
    stdin: {
      read: async () => (options.read ?? (() => Buffer.alloc(0)))(),
    },
    stdout: {
      write: (data: Uint8Array) => {
        if (options.writeThrows === true) {
          throw new Error("channel closed");
        }
        options.onWrite?.(data);
      },
      drain: async () => {
        if (options.drainThrows === true) {
          throw new Error("broken pipe");
        }
        state.drained += 1;
      },
    },
    exit: (code: number) => state.exited.push(code),
    close: () => {
      state.closed += 1;
    },
    getExtraInfo: (name: string) => (name === "peername" ? options.peer : undefined),
  };
}

describe("reading from a session", () => {
  it.each(golden.reads)("$name", async (record) => {
    const reader = new SshStreamReader(makeProcess({ read: READ_VALUES[record.name] }));
    expect([...(await reader.read())]).toEqual(record.result);
  });

  it("hands back the bytes it was given", async () => {
    const reader = new SshStreamReader(makeProcess({ read: () => Buffer.from("hello") }));
    expect(Buffer.from(await reader.read()).toString()).toBe("hello");
  });

  it("encodes text rather than refusing it", async () => {
    // A channel negotiated with an encoding hands back a string; the platform
    // below deals in bytes, and refusing would drop a live session over a
    // negotiation detail.
    const reader = new SshStreamReader(makeProcess({ read: () => "héllo" }));
    expect(Buffer.from(await reader.read()).toString("utf8")).toBe("héllo");
  });

  it("replaces a character it cannot encode with a question mark", async () => {
    // Not U+FFFD, which is what *decoding* substitutes. A lone surrogate must
    // not end a session.
    const reader = new SshStreamReader(makeProcess({ read: () => "a\udc80b" }));
    expect(Buffer.from(await reader.read()).toString()).toBe("a?b");
  });

  it("treats every failure as end of stream", async () => {
    // A dropped connection, a cancelled read and a clean EOF all mean the
    // same thing to the caller: no more bytes.
    for (const name of ["a closed connection", "an end of file", "a cancelled read"]) {
      const reader = new SshStreamReader(makeProcess({ read: READ_VALUES[name] }));
      expect([...(await reader.read())]).toEqual([]);
    }
  });

  it("asks for the number of bytes it was asked for", async () => {
    // The caller's bound, not the adapter's: a reader that always asked for
    // everything would block a caller that wanted one frame.
    const asked: Array<number | undefined> = [];
    const process = makeProcess({});
    process.stdin.read = async (size?: number) => {
      asked.push(size);
      return Buffer.alloc(0);
    };
    const reader = new SshStreamReader(process);
    await reader.read(64);
    await reader.read();
    expect(asked).toEqual([64, -1]);
  });

  it("treats a value it does not understand as end of stream", async () => {
    for (const value of [7, null, undefined, {}]) {
      const reader = new SshStreamReader(makeProcess({ read: () => value }));
      expect([...(await reader.read())]).toEqual([]);
    }
  });
});

describe("writing to a session", () => {
  it("passes bytes through and flushes", async () => {
    const written: number[][] = [];
    const process = makeProcess({ onWrite: (data) => written.push([...data]) });
    const writer = new SshStreamWriter(process);
    writer.write(Buffer.from("one"));
    await writer.drain();
    writer.write(Buffer.from("two"));
    expect(written).toEqual(golden.writes.live.written);
    expect(process.drained).toBe(golden.writes.live.drained);
  });

  it("closes itself when a write fails", async () => {
    // There is nowhere left to send anything, and continuing would raise once
    // per frame for the life of the session.
    const process = makeProcess({ writeThrows: true });
    const writer = new SshStreamWriter(process);
    writer.write(Buffer.from("one"));
    writer.write(Buffer.from("two"));
    await writer.drain();
    expect(process.exited).toEqual(golden.writes.write_failure.exited);
    expect(process.closed).toBe(golden.writes.write_failure.closed);
  });

  it("closes itself when a flush fails", async () => {
    const process = makeProcess({ drainThrows: true });
    const writer = new SshStreamWriter(process);
    await writer.drain();
    expect(process.exited).toEqual(golden.writes.drain_failure.exited);
    expect(process.closed).toBe(golden.writes.drain_failure.closed);
  });

  it("does nothing at all once closed", async () => {
    // The session that closed it has already moved on, and an error raised
    // there has nobody to catch it.
    const written: number[][] = [];
    const process = makeProcess({ onWrite: (data) => written.push([...data]) });
    const writer = new SshStreamWriter(process);
    writer.close();
    writer.write(Buffer.from("after"));
    await writer.drain();
    writer.close();
    expect(written).toEqual(golden.writes.after_close.written);
    expect(process.drained).toBe(golden.writes.after_close.drained);
    // Closed once, not twice — the second close is a no-op.
    expect(process.exited).toEqual(golden.writes.after_close.exited);
    expect(process.closed).toBe(golden.writes.after_close.closed);
  });

  it("exits the process cleanly on close", async () => {
    // A session that ends without an exit status leaves the client waiting.
    const process = makeProcess({});
    new SshStreamWriter(process).close();
    expect(process.exited).toEqual([0]);
  });

  it("survives a process that cannot be closed", () => {
    // Closing is best-effort: the connection may already be gone, and the
    // writer must still mark itself closed.
    const process = {
      ...makeProcess({}),
      exit: () => {
        throw new Error("already gone");
      },
      close: () => {
        throw new Error("already gone");
      },
    };
    const writer = new SshStreamWriter(process);
    expect(() => writer.close()).not.toThrow();
    expect(() => writer.write(Buffer.from("after"))).not.toThrow();
  });

  it("waits for a close that has already happened", async () => {
    // The runtime manages its own lifecycle, so there is nothing to wait for.
    await expect(new SshStreamWriter(makeProcess({})).waitClosed()).resolves.toBeUndefined();
  });
});

describe("what the writer reports about the other end", () => {
  it.each(golden.peers)("$name", (record) => {
    const writer = new SshStreamWriter(makeProcess({ peer: record.peer }));
    expect(writer.getExtraInfo("peername") ?? null).toEqual(record.result);
    expect(writer.getExtraInfo("sockname", "fallback")).toBe(record.other);
  });

  it("reports the peer address", () => {
    const writer = new SshStreamWriter(makeProcess({ peer: ["10.0.0.2", 5003] }));
    expect(writer.getExtraInfo("peername")).toEqual(["10.0.0.2", 5003]);
  });

  it("takes the default for an empty peer", () => {
    // An empty address is no address; reporting it would put a blank string
    // in an audit line.
    expect(new SshStreamWriter(makeProcess({ peer: [] })).getExtraInfo("peername", "none")).toBe("none");
    expect(new SshStreamWriter(makeProcess({ peer: null })).getExtraInfo("peername", "none")).toBe("none");
  });

  it("knows nothing else about the connection", () => {
    const writer = new SshStreamWriter(makeProcess({ peer: ["10.0.0.2", 5003] }));
    expect(writer.getExtraInfo("sockname", "fallback")).toBe("fallback");
    expect(writer.getExtraInfo("cipher")).toBeUndefined();
  });
});
