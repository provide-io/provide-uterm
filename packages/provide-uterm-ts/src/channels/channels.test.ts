//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { encodeControlFrame } from "../control-channel/index.ts";
import { loadGolden } from "../testing/golden.ts";
import { NegotiatedChannels, parseChannelHello } from "./index.ts";

interface ChannelsGolden {
  negotiate: Array<{
    name: string;
    supported: Record<string, number>;
    requested: Record<string, number>;
    ack: Record<string, unknown>;
    granted: Record<string, number>;
    exported: Record<string, number>;
  }>;
  parse: Array<{ name: string; raw: string; channels: Record<string, number> | null }>;
  sequence: {
    steps: Array<{ channel: string | null; seq: number }>;
    restored_granted: Record<string, number>;
    steps_after_restore: Array<{ channel: string; seq: number }>;
  };
  ack_fields: Array<{ extra: Record<string, unknown>; ack: Record<string, unknown> | null; error: string | null }>;
  errors: Array<{ name: string; error: string | null }>;
  is_negotiated: Array<Record<string, unknown>>;
}

const golden = loadGolden<ChannelsGolden>("channels_golden.json");

/** Every channel map the reference refuses to coerce, in corpus order. */
const INVALID_CHANNEL_MAPS: unknown[] = [
  null,
  [],
  "term",
  1,
  { "": 1 },
  { term: "1" },
  { term: 1.5 },
  { term: null },
  { term: true },
  { term: false },
];

describe("NegotiatedChannels construction", () => {
  it("requires at least one supported channel", () => {
    expect(() => new NegotiatedChannels({})).toThrow("at least one supported channel is required");
  });

  it("requires the default channel to be supported", () => {
    expect(() => new NegotiatedChannels({ term: 1 }, { defaultChannel: "gui" })).toThrow(
      'default channel is not supported: "gui"',
    );
  });

  it("accepts a supported channel as the default", () => {
    expect(() => new NegotiatedChannels({ term: 1 }, { defaultChannel: "term" })).not.toThrow();
  });

  it.each([
    ["null", null, "channels must be a mapping"],
    ["an array", [], "channels must be a mapping"],
    ["a string", "term", "channels must be a mapping"],
    ["a number", 1, "channels must be a mapping"],
    ["an empty channel name", { "": 1 }, "channel names must be non-empty strings"],
    ["a string version", { term: "1" }, "channel versions must be integers"],
    ["a fractional version", { term: 1.5 }, "channel versions must be integers"],
    ["a null version", { term: null }, "channel versions must be integers"],
    ["a true version", { term: true }, "channel versions must be integers"],
    ["a false version", { term: false }, "channel versions must be integers"],
  ])("refuses %s as a supported map", (_name, value, message) => {
    expect(() => new NegotiatedChannels(value as Record<string, number>)).toThrow(message as string);
  });

  it("refuses an array, which is an object in JavaScript but not a mapping", () => {
    expect(() => new NegotiatedChannels([] as unknown as Record<string, number>)).toThrow("channels must be a mapping");
  });
});

describe("NegotiatedChannels negotiation", () => {
  it("grants the lower of the requested and supported versions", () => {
    const channels = new NegotiatedChannels({ term: 3 });
    expect(channels.handleHello({ type: "hello", channels: { term: 2 } })).toStrictEqual({
      type: "hello_ack",
      channels: { term: 2 },
    });
  });

  it("caps a request above what the server supports", () => {
    const channels = new NegotiatedChannels({ term: 2 });
    expect(channels.granted).toStrictEqual({});
    channels.handleHello({ type: "hello", channels: { term: 5 } });
    expect(channels.granted).toStrictEqual({ term: 2 });
  });

  it("drops a channel the server does not support", () => {
    const channels = new NegotiatedChannels({ term: 1 });
    channels.handleHello({ type: "hello", channels: { other: 1 } });
    expect(channels.granted).toStrictEqual({});
  });

  it("refuses a version of zero or below", () => {
    const channels = new NegotiatedChannels({ term: 1 });
    channels.handleHello({ type: "hello", channels: { term: 0 } });
    expect(channels.granted).toStrictEqual({});
    channels.handleHello({ type: "hello", channels: { term: -1 } });
    expect(channels.granted).toStrictEqual({});
  });

  it("returns a copy of the grant map, not the live one", () => {
    const channels = new NegotiatedChannels({ term: 1 });
    channels.handleHello({ type: "hello", channels: { term: 1 } });
    const grants = channels.granted;
    grants.term = 99;
    expect(channels.granted).toStrictEqual({ term: 1 });
  });

  it("accepts a ChannelHello as well as a raw payload", () => {
    const channels = new NegotiatedChannels({ term: 2 });
    expect(channels.handleHello({ channels: { term: 1 } })).toStrictEqual({
      type: "hello_ack",
      channels: { term: 1 },
    });
  });

  it("merges extra ack fields", () => {
    const channels = new NegotiatedChannels({ term: 1 });
    expect(channels.handleHello({ type: "hello", channels: { term: 1 } }, { sessionId: "s1" })).toStrictEqual({
      type: "hello_ack",
      channels: { term: 1 },
      sessionId: "s1",
    });
  });

  it("refuses an ack field that would overwrite a reserved name", () => {
    const channels = new NegotiatedChannels({ term: 1 });
    expect(() => channels.handleHello({ type: "hello", channels: {} }, { type: "x" })).toThrow(
      "reserved hello_ack field: type",
    );
    expect(() => channels.handleHello({ type: "hello", channels: {} }, { channels: {} })).toThrow(
      "reserved hello_ack field: channels",
    );
  });

  it("names the first reserved field in sorted order when both are present", () => {
    const channels = new NegotiatedChannels({ term: 1 });
    expect(() => channels.handleHello({ type: "hello", channels: {} }, { type: "x", channels: {} })).toThrow(
      "reserved hello_ack field: channels",
    );
  });
});

describe("NegotiatedChannels sequencing", () => {
  it("starts each channel counter at one and increments per channel", () => {
    const channels = new NegotiatedChannels({ term: 1, gui: 1 }, { defaultChannel: "term" });
    expect(channels.nextSeq()).toBe(1);
    expect(channels.nextSeq()).toBe(2);
    expect(channels.nextSeq("gui")).toBe(1);
    expect(channels.nextSeq("term")).toBe(3);
  });

  it("counts a channel that was never granted", () => {
    const channels = new NegotiatedChannels({ term: 1 }, { defaultChannel: "term" });
    expect(channels.nextSeq("other")).toBe(1);
  });

  it("requires a channel name when no default is configured", () => {
    const channels = new NegotiatedChannels({ term: 1 });
    expect(() => channels.nextSeq()).toThrow("channel is required when no default_channel is configured");
    expect(() => channels.isNegotiated()).toThrow("channel is required when no default_channel is configured");
  });

  it("resets sequence counters when grants are restored", () => {
    const channels = new NegotiatedChannels({ term: 1 }, { defaultChannel: "term" });
    channels.handleHello({ type: "hello", channels: { term: 1 } });
    channels.nextSeq();
    channels.restoreGrants({ term: 1 });
    expect(channels.nextSeq()).toBe(1);
  });

  it("re-negotiates restored grants against what is supported", () => {
    const channels = new NegotiatedChannels({ term: 1 }, { defaultChannel: "term" });
    channels.restoreGrants({ term: 9, gone: 1 });
    expect(channels.granted).toStrictEqual({ term: 1 });
  });
});

describe("NegotiatedChannels.isNegotiated", () => {
  it("resolves the default channel when none is named", () => {
    const channels = new NegotiatedChannels({ term: 1, gui: 1 }, { defaultChannel: "term" });
    expect(channels.isNegotiated()).toBe(false);
    channels.handleHello({ type: "hello", channels: { term: 1 } });
    expect(channels.isNegotiated()).toBe(true);
    expect(channels.isNegotiated("gui")).toBe(false);
    expect(channels.isNegotiated("nope")).toBe(false);
  });
});

describe("parseChannelHello", () => {
  it("returns the advertised channels from a hello frame", () => {
    const frame = encodeControlFrame({ type: "hello", channels: { term: 2 } });
    expect(parseChannelHello(frame)?.channels).toStrictEqual({ term: 2 });
  });

  it("returns null for an empty string", () => {
    expect(parseChannelHello("")).toBeNull();
  });

  it("returns null for text that is not a frame", () => {
    expect(parseChannelHello("not a frame")).toBeNull();
  });

  it("returns null for a frame whose type is not hello", () => {
    expect(parseChannelHello(encodeControlFrame({ type: "hello_ack", channels: { term: 1 } }))).toBeNull();
  });

  it("returns null when the channels field is missing or unusable", () => {
    expect(parseChannelHello(encodeControlFrame({ type: "hello" }))).toBeNull();
    expect(parseChannelHello(encodeControlFrame({ type: "hello", channels: { term: "x" } }))).toBeNull();
    expect(parseChannelHello(encodeControlFrame({ type: "hello", channels: [1] }))).toBeNull();
  });

  it("returns an empty map for a hello that advertises nothing", () => {
    expect(parseChannelHello(encodeControlFrame({ type: "hello", channels: {} }))?.channels).toStrictEqual({});
  });

  it("returns null rather than throwing on a malformed frame", () => {
    expect(parseChannelHello("\x10\x0200000003:abc")).toBeNull();
    expect(parseChannelHello(encodeControlFrame({ type: "hello" }).slice(0, -1))).toBeNull();
  });
});

describe("differential parity with CPython", () => {
  it("matches every negotiation record", () => {
    for (const record of golden.negotiate) {
      const defaultChannel = Object.keys(record.supported)[0] as string;
      const channels = new NegotiatedChannels(record.supported, { defaultChannel });
      const ack = channels.handleHello({ type: "hello", channels: record.requested });
      expect({ name: record.name, ack, granted: channels.granted, exported: channels.exportGrants() }).toStrictEqual({
        name: record.name,
        ack: record.ack,
        granted: record.granted,
        exported: record.exported,
      });
    }
    expect(golden.negotiate.length).toBeGreaterThan(8);
  });

  it("matches every parse record", () => {
    for (const record of golden.parse) {
      const hello = parseChannelHello(record.raw);
      expect({ name: record.name, channels: hello === null ? null : hello.channels }).toStrictEqual({
        name: record.name,
        channels: record.channels,
      });
    }
    expect(golden.parse.length).toBeGreaterThan(10);
  });

  it("matches the recorded sequence walk", () => {
    const channels = new NegotiatedChannels({ term: 1, gui: 1 }, { defaultChannel: "term" });
    channels.handleHello({ type: "hello", channels: { term: 1, gui: 1 } });
    const steps = golden.sequence.steps.map((step) => ({
      channel: step.channel,
      seq: step.channel === null ? channels.nextSeq() : channels.nextSeq(step.channel),
    }));
    expect(steps).toStrictEqual(golden.sequence.steps);

    const restored = new NegotiatedChannels({ term: 1, gui: 1 }, { defaultChannel: "term" });
    restored.restoreGrants(channels.exportGrants());
    expect(restored.granted).toStrictEqual(golden.sequence.restored_granted);
    expect(
      golden.sequence.steps_after_restore.map((step) => ({
        channel: step.channel,
        seq: restored.nextSeq(step.channel),
      })),
    ).toStrictEqual(golden.sequence.steps_after_restore);
  });

  it("matches every recorded error message", () => {
    const factories: Array<() => unknown> = [
      () => new NegotiatedChannels({}),
      () => new NegotiatedChannels({ term: 1 }, { defaultChannel: "gui" }),
      () => new NegotiatedChannels({ term: 1 }).nextSeq(),
      () => new NegotiatedChannels({ term: 1 }).isNegotiated(),
      ...INVALID_CHANNEL_MAPS.map((value) => () => new NegotiatedChannels(value as Record<string, number>)),
    ];
    const actual = golden.errors.map((record, index) => {
      let error: string | null = null;
      try {
        (factories[index] as () => unknown)();
      } catch (caught) {
        error = (caught as Error).message;
      }
      return { name: record.name, error };
    });
    // CPython renders the offending channel name with repr(), which uses
    // single quotes; JSON.stringify uses double quotes. Compare the rest.
    expect(actual.map((r) => r.error?.replace(/"/g, "'") ?? null)).toStrictEqual(golden.errors.map((r) => r.error));
  });
});
