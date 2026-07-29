//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  DEFAULT_UPDATE_DRIVE_INTERVAL_S,
  DRIVE_FBUR,
  DRIVE_HANDSHAKE_WAIT_S,
  isShutdownRace,
  JOIN_TIMEOUT_S,
  PUMP_CHUNK,
  type RelayStreams,
  runHumanRelay,
} from "./index.ts";

interface RelayGolden {
  pump_chunk: number;
  join_timeout_s: number;
  default_update_drive_interval_s: number;
  drive_handshake_wait_s: number;
  drive_fbur: number[];
  unblock: Array<{ name: string; closed: boolean }>;
  close_failure_tolerated: boolean;
  pump_errors: Array<{ error: string; reraised: boolean }>;
}

const golden = loadGolden<RelayGolden>("vncrelay_golden.json");

/**
 * A writer whose every write spans two turns of the event loop.
 *
 * Interleaving is only observable if a write takes time: a single synchronous
 * call can never be split. This records a `begin`/`end` pair around an awaited
 * gap, so anything writing concurrently shows up between them.
 */
class SlowRecorder {
  readonly events: string[] = [];
  #next = 0;

  write(data: Uint8Array): void {
    const id = this.#next;
    this.#next += 1;
    this.events.push(`begin:${id}:${data.length}`);
    // Resolved on a later turn, which is when a rival writer would get in.
    void Promise.resolve().then(() => {
      this.events.push(`end:${id}`);
    });
  }

  /** Whether every write closed before the next one opened. */
  interleaved(): boolean {
    const open: string[] = [];
    for (const event of this.events) {
      const [kind, id] = event.split(":");
      if (kind === "begin") {
        if (open.length > 0) {
          return true;
        }
        open.push(id as string);
      } else {
        open.pop();
      }
    }
    return false;
  }
}

/** Everything written, in the order it was written. */
class Recorder {
  readonly writes: Uint8Array[] = [];
  flushes = 0;

  write(data: Uint8Array): void {
    this.writes.push(Uint8Array.from(data));
  }

  flush(): void {
    this.flushes += 1;
  }

  /** What was written, as one string of latin-1 characters. */
  text(): string {
    return this.writes
      .flatMap((chunk) => [...chunk])
      .map((byte) => String.fromCharCode(byte))
      .join("");
  }
}

/** An upstream that hands over fixed chunks and then ends. */
function upstreamOf(
  chunks: string[],
  fail?: Error,
): {
  read(size: number): Promise<Uint8Array>;
  readonly sizes: number[];
} {
  let pending = chunks.join("");
  const sizes: number[] = [];
  return {
    sizes,
    async read(size: number): Promise<Uint8Array> {
      // The size is honoured, so a pump asking for the wrong amount shows up
      // in what the browser receives per write.
      sizes.push(size);
      if (pending === "") {
        if (fail !== undefined) {
          throw fail;
        }
        return new Uint8Array(0);
      }
      const chunk = pending.slice(0, size);
      pending = pending.slice(size);
      return Uint8Array.from([...chunk].map((character) => character.charCodeAt(0)));
    },
  };
}

/** The four streams, with the two written ones recorded. */
function streamsFor(
  upstreamChunks: string[],
  fail?: Error,
): RelayStreams & {
  browser: Recorder;
  upstream: Recorder;
  reads: number[];
} {
  const browser = new Recorder();
  const upstream = new Recorder();
  const source = upstreamOf(upstreamChunks, fail);
  return {
    browser,
    upstream,
    reads: source.sizes,
    browserRead: { read: () => new Uint8Array(0) },
    browserWrite: browser,
    upstreamRead: source,
    upstreamWrite: upstream,
  };
}

/** A relay whose browser side runs for a controlled number of turns. */
function relayOptions(overrides: Partial<Parameters<typeof runHumanRelay>[1]> = {}) {
  return {
    sessionId: "s",
    leaseId: "l",
    principalId: "p",
    principalRole: "operator",
    canInject: () => true,
    // A driver's interval becomes one turn of the event loop rather than a
    // wall-clock wait — a *turn*, not a resolved promise, because a microtask
    // that resolves at once would starve everything else and the driver would
    // spin. The handshake timeout never returns, which is what "it has not
    // elapsed yet" looks like.
    sleep: async (seconds: number) => {
      if (seconds === DRIVE_HANDSHAKE_WAIT_S) {
        await new Promise(() => undefined);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
    },
    ...overrides,
  };
}

describe("what the relay moves", () => {
  it("pumps upstream to the browser and flushes each chunk", async () => {
    // A live RFB peer sends its version banner and then waits, so a buffered
    // writer would leave the browser staring at nothing until the upstream
    // closed.
    const streams = streamsFor(["RFB 003.008\n", "more"]);
    const result = await runHumanRelay(streams, relayOptions({ filterInput: async () => undefined }));
    expect(streams.browser.text()).toBe("RFB 003.008\nmore");
    // One flush per write, whatever the writes turn out to be.
    expect(streams.browser.flushes).toBe(streams.browser.writes.length);
    expect(result.upstreamEnded).toBe(true);
  });

  it("moves a long stream in the chunks the reference uses", async () => {
    // Which is the size it asks for, not whatever the source felt like
    // handing over.
    const streams = streamsFor(["x".repeat(PUMP_CHUNK + 100)]);
    await runHumanRelay(streams, relayOptions({ filterInput: async () => undefined }));
    expect(streams.reads.every((size) => size === PUMP_CHUNK)).toBe(true);
    expect(streams.browser.writes.map((chunk) => chunk.length)).toEqual([PUMP_CHUNK, 100]);
    expect(streams.browser.flushes).toBe(2);
  });

  it("tells its owner once the upstream has drained", async () => {
    // Otherwise the browser side stays parked reading an idle browser forever
    // after the server has gone.
    const seen: string[] = [];
    const streams = streamsFor(["a"]);
    await runHumanRelay(
      streams,
      relayOptions({
        filterInput: async () => undefined,
        onUpstreamEof: () => seen.push("eof"),
      }),
    );
    expect(seen).toEqual(["eof"]);
  });

  it("fires that callback only once everything has been delivered", async () => {
    // Before draining, the owner would tear the browser side down with bytes
    // still in flight.
    let seenAtEof = "";
    const streams = streamsFor(["one", "two"]);
    await runHumanRelay(
      streams,
      relayOptions({
        filterInput: async () => undefined,
        onUpstreamEof: () => {
          seenAtEof = streams.browser.text();
        },
      }),
    );
    expect(seenAtEof).toBe("onetwo");
  });

  it("does not let that callback take the pump down", async () => {
    const streams = streamsFor(["a"]);
    const result = await runHumanRelay(
      streams,
      relayOptions({
        filterInput: async () => undefined,
        onUpstreamEof: () => {
          throw new Error("owner blew up");
        },
      }),
    );
    expect(result.upstreamEnded).toBe(true);
  });

  it("reads in the chunks the reference reads in", () => {
    expect(PUMP_CHUNK).toBe(golden.pump_chunk);
    expect(JOIN_TIMEOUT_S).toBe(golden.join_timeout_s);
  });
});

describe("the update driver", () => {
  it("waits for the client's first request before injecting anything", async () => {
    // The client's pixel format and encodings precede that request; injecting
    // earlier would have the server answer in its own format and the client
    // render those frames with swapped colours.
    const streams = streamsFor([]);
    const result = await runHumanRelay(
      streams,
      relayOptions({
        driveUpdateIntervalS: 0.01,
        // Never signals readiness, so the driver must inject nothing.
        filterInput: async () => undefined,
      }),
    );
    expect(result.driven).toBe(0);
    expect(streams.upstream.writes).toHaveLength(0);
  });

  it("injects once the client is ready", async () => {
    const streams = streamsFor([]);
    let turns = 0;
    const result = await runHumanRelay(
      streams,
      relayOptions({
        driveUpdateIntervalS: 0.01,
        filterInput: async ({ onClientReady }) => {
          onClientReady();
          // Give the driver a few turns before the browser side finishes.
          while (turns < 3) {
            turns += 1;
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
        },
      }),
    );
    expect(result.driven).toBeGreaterThan(0);
    expect([...(streams.upstream.writes[0] as Uint8Array)]).toEqual(golden.drive_fbur);
  });

  it("injects the request the reference injects", () => {
    // Whole surface, incremental, width and height left at their sixteen-bit
    // maximum for the server to clamp.
    expect([...DRIVE_FBUR]).toEqual(golden.drive_fbur);
    expect(DRIVE_FBUR[0]).toBe(3);
    expect(DRIVE_FBUR[1]).toBe(1);
  });

  it("does not run at all when nobody asked for it", async () => {
    for (const interval of [undefined, 0, -1]) {
      const streams = streamsFor([]);
      const result = await runHumanRelay(
        streams,
        relayOptions({
          driveUpdateIntervalS: interval,
          filterInput: async ({ onClientReady }) => {
            onClientReady();
          },
        }),
      );
      expect(result.driven).toBe(0);
    }
  });

  it("stops before anything is torn down", async () => {
    // So it cannot write into a stream being closed.
    const streams = streamsFor([]);
    const result = await runHumanRelay(
      streams,
      relayOptions({
        driveUpdateIntervalS: 0.01,
        filterInput: async ({ onClientReady }) => {
          onClientReady();
          await new Promise((resolve) => setTimeout(resolve, 0));
        },
      }),
    );
    const after = streams.upstream.writes.length;
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(streams.upstream.writes).toHaveLength(after);
    expect(result.driven).toBe(after);
  });

  it("gives up when the client never asks for an update", async () => {
    // Bounded rather than waiting forever: a client that never asks is one
    // the driver has nothing to do for. Here the handshake timeout elapses
    // first and the driver injects nothing at all.
    const streams = streamsFor([]);
    const result = await runHumanRelay(
      streams,
      relayOptions({
        driveUpdateIntervalS: 0.01,
        sleep: async () => undefined,
        filterInput: async () => {
          await new Promise((resolve) => setTimeout(resolve, 0));
        },
      }),
    );
    expect(result.driven).toBe(0);
    expect(streams.upstream.writes).toHaveLength(0);
  });

  it("stops driving when a write upstream fails", async () => {
    // The upstream has gone; the relay ends rather than retrying into a
    // stream that is not there.
    const streams = streamsFor([]);
    const error = Object.assign(new Error("gone"), { name: "OSError" });
    const result = await runHumanRelay(
      {
        ...streams,
        upstreamWrite: {
          write: () => {
            throw error;
          },
        },
      },
      relayOptions({
        driveUpdateIntervalS: 0.01,
        filterInput: async ({ onClientReady }) => {
          onClientReady();
          await new Promise((resolve) => setTimeout(resolve, 2));
        },
      }),
    );
    expect(result.driven).toBe(0);
    expect(result.races).toContain(error);
    // Ended rather than retried: exactly one attempt, and one race recorded.
    expect(result.races).toHaveLength(1);
  });

  it("counts only the requests that actually went", async () => {
    // A write that failed did not reach the upstream, so counting it would
    // report motion that never happened.
    const streams = streamsFor([]);
    let attempts = 0;
    const result = await runHumanRelay(
      {
        ...streams,
        upstreamWrite: {
          write: (data: Uint8Array) => {
            attempts += 1;
            if (attempts === 2) {
              throw Object.assign(new Error("gone"), { name: "OSError" });
            }
            streams.upstream.write(data);
          },
        },
      },
      relayOptions({
        driveUpdateIntervalS: 0.01,
        filterInput: async ({ onClientReady }) => {
          onClientReady();
          for (let turn = 0; turn < 6; turn += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
        },
      }),
    );
    expect(attempts).toBe(2);
    expect(result.driven).toBe(1);
  });

  it("waits on the real clock when it is given none", async () => {
    // The default: a caller that supplies no clock gets one, and a short
    // interval is a short wait rather than a spin.
    const streams = streamsFor([]);
    const result = await runHumanRelay(streams, {
      sessionId: "s",
      leaseId: "l",
      principalId: "p",
      principalRole: "operator",
      canInject: () => true,
      driveUpdateIntervalS: 0.001,
      filterInput: async ({ onClientReady }) => {
        onClientReady();
        await new Promise((resolve) => setTimeout(resolve, 5));
      },
    });
    expect(result.driven).toBeGreaterThan(0);
  });

  it("waits the number of seconds the reference waits", async () => {
    // Bounded rather than waiting forever: a client that never asks for an
    // update is one the driver has nothing to do for.
    expect(DRIVE_HANDSHAKE_WAIT_S).toBe(golden.drive_handshake_wait_s);
    expect(DEFAULT_UPDATE_DRIVE_INTERVAL_S).toBe(golden.default_update_drive_interval_s);
  });

  it("keeps injecting, rather than doing it once", async () => {
    // A driver that stopped after one request would leave an animating screen
    // frozen on frame two instead of frame one.
    const streams = streamsFor([]);
    const result = await runHumanRelay(
      streams,
      relayOptions({
        driveUpdateIntervalS: 0.01,
        filterInput: async ({ onClientReady }) => {
          onClientReady();
          for (let turn = 0; turn < 6; turn += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
        },
      }),
    );
    expect(result.driven).toBeGreaterThanOrEqual(2);
  });

  it("never lets a write upstream be split by the other writer", async () => {
    // A message split down the middle by the other is not a message. Only
    // observable with a writer whose write spans a turn, which is what a real
    // socket's does.
    const slow = new SlowRecorder();
    const streams = streamsFor([]);
    await runHumanRelay(
      { ...streams, upstreamWrite: slow },
      relayOptions({
        driveUpdateIntervalS: 0.01,
        filterInput: async ({ onClientReady, write }) => {
          onClientReady();
          // Through the relay's own write, which is the bargain: the
          // guarantee covers what goes through it.
          for (let turn = 0; turn < 4; turn += 1) {
            await write(new Uint8Array([1, 2, 3]));
          }
        },
      }),
    );
    expect(slow.events.length).toBeGreaterThan(4);
    expect(slow.interleaved()).toBe(false);
  });

  it("leaves no driver still running when it returns", async () => {
    // Not merely quiet: waited for. A driver left running is work outliving
    // the session that owns it.
    let returned = false;
    const lateWaits: number[] = [];
    const streams = streamsFor([]);
    await runHumanRelay(
      streams,
      relayOptions({
        driveUpdateIntervalS: 0.01,
        sleep: async (seconds: number) => {
          if (seconds === DRIVE_HANDSHAKE_WAIT_S) {
            await new Promise(() => undefined);
            return;
          }
          if (returned) {
            lateWaits.push(seconds);
          }
          await new Promise((resolve) => setTimeout(resolve, 0));
        },
        filterInput: async ({ onClientReady }) => {
          onClientReady();
          for (let turn = 0; turn < 4; turn += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
        },
      }),
    );
    returned = true;
    for (let turn = 0; turn < 5; turn += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    expect(lateWaits).toEqual([]);
  });

  it("leaves no pump still running when it returns", async () => {
    // The same for the upstream side: a read landing after the relay returned
    // would write into a browser its owner has already torn down.
    let returned = false;
    const late: string[] = [];
    const browser = new Recorder();
    let reads = 0;
    await runHumanRelay(
      {
        browserRead: { read: () => new Uint8Array(0) },
        browserWrite: {
          write: (data: Uint8Array) => {
            if (returned) {
              late.push("write");
            }
            browser.write(data);
          },
        },
        upstreamRead: {
          async read(): Promise<Uint8Array> {
            reads += 1;
            // Slow enough that the browser side finishes first.
            await new Promise((resolve) => setTimeout(resolve, 1));
            return reads <= 2 ? Uint8Array.from([65]) : new Uint8Array(0);
          },
        },
        upstreamWrite: new Recorder(),
      },
      relayOptions({ filterInput: async () => undefined }),
    );
    returned = true;
    for (let turn = 0; turn < 5; turn += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    expect(late).toEqual([]);
    expect(browser.text()).toBe("AA");
  });

  it("writes nothing more once the relay has returned", async () => {
    // The driver stops before anything is torn down, so it cannot write into
    // a stream being closed.
    const streams = streamsFor([]);
    const result = await runHumanRelay(
      streams,
      relayOptions({
        driveUpdateIntervalS: 0.01,
        filterInput: async ({ onClientReady }) => {
          onClientReady();
          for (let turn = 0; turn < 4; turn += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
        },
      }),
    );
    const written = streams.upstream.writes.length;
    expect(result.driven).toBe(written);
    // Several turns later, still nothing: the driver is not merely paused.
    for (let turn = 0; turn < 5; turn += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    expect(streams.upstream.writes).toHaveLength(written);
  });
});

describe("when the upstream goes away", () => {
  it.each(golden.pump_errors)("$error", async (record) => {
    const error = Object.assign(new Error("gone"), { name: record.error });
    const streams = streamsFor(["a"], error);
    const run = runHumanRelay(streams, relayOptions({ filterInput: async () => undefined }));
    if (record.reraised) {
      await expect(run).rejects.toThrow("gone");
      return;
    }
    const result = await run;
    expect(result.races).toContain(error);
  });

  it("treats a closed pipe as the ordinary end of a relay", async () => {
    // Which is how this stops when a session is torn down.
    for (const name of ["OSError", "ValueError", "AbortError"]) {
      expect(isShutdownRace(Object.assign(new Error("x"), { name }))).toBe(true);
    }
  });

  it("passes a real fault on", async () => {
    for (const name of ["RuntimeError", "KeyError", "Error"]) {
      expect(isShutdownRace(Object.assign(new Error("x"), { name }))).toBe(false);
    }
  });

  it("tells its owner about a race rather than failing", async () => {
    const seen: unknown[] = [];
    const error = Object.assign(new Error("gone"), { name: "OSError" });
    const streams = streamsFor(["a"], error);
    await runHumanRelay(
      streams,
      relayOptions({ filterInput: async () => undefined, onShutdownRace: (raced) => seen.push(raced) }),
    );
    expect(seen).toEqual([error]);
  });

  it("still delivers what it read before the failure", async () => {
    const error = Object.assign(new Error("gone"), { name: "OSError" });
    const streams = streamsFor(["banner"], error);
    await runHumanRelay(streams, relayOptions({ filterInput: async () => undefined }));
    expect(streams.browser.text()).toBe("banner");
  });
});

describe("relaying a buffered session end to end", () => {
  it("filters the browser's input on its way upstream", async () => {
    // The default path, with the synchronous filter: right for a buffered
    // source, and the reason a live one supplies its own.
    const handshake = [..."RFB 003.008\n"].map((character) => character.charCodeAt(0));
    const input = Uint8Array.from([...handshake, 1, 1, 4, 0, 0, 0, 0, 0, 0, 0]);
    let offset = 0;
    const streams = streamsFor([]);
    await runHumanRelay(
      {
        ...streams,
        browserRead: {
          read: (size) => {
            const chunk = input.subarray(offset, offset + size);
            offset += chunk.length;
            return chunk;
          },
        },
      },
      relayOptions(),
    );
    // The handshake passed through and the keystroke was forwarded, because
    // this caller may inject.
    expect(streams.upstream.text().startsWith("RFB 003.008\n")).toBe(true);
  });

  it("forwards nothing a viewer may not send", async () => {
    const handshake = [..."RFB 003.008\n"].map((character) => character.charCodeAt(0));
    const input = Uint8Array.from([...handshake, 1, 1, 4, 0, 0, 0, 0, 0, 0, 0]);
    let offset = 0;
    const streams = streamsFor([]);
    await runHumanRelay(
      {
        ...streams,
        browserRead: {
          read: (size) => {
            const chunk = input.subarray(offset, offset + size);
            offset += chunk.length;
            return chunk;
          },
        },
      },
      relayOptions({ canInject: () => false }),
    );
    // The handshake still passes; the keystroke does not.
    expect(streams.upstream.text()).toBe("RFB 003.008\n");
  });
});
