//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  clientMetadata,
  DEFAULT_QUEUE_CAPACITY,
  DEFAULT_TELNET_POLICY,
  filterMatches,
  INTERCEPT_ACTIONS,
  interceptConsume,
  interceptDefer,
  interceptInject,
  interceptPass,
  interceptReplace,
  onTelnetOption,
  onTelnetSubnegotiation,
} from "./index.ts";

interface EmbedGolden {
  default_terminal_type: string;
  default_window_size: number[];
  default_backpressure: string;
  default_queue_capacity: number;
  intercept_actions: string[];
  intercept_defaults: Record<string, [string, number[] | null]>;
  filters: Array<{
    name: string;
    tags: string[];
    require_any_tag: string[] | null;
    exclude_tags: string[] | null;
    result: boolean;
  }>;
  options: Array<{ name: string; command: number; option: number; reply: number[] }>;
  subnegotiations: Array<{ name: string; option: number; body: number[]; reply: number[]; wide_reply: number[] }>;
  unencodable_terminal_type: number[];
  wide_window: number[];
}

const golden = loadGolden<EmbedGolden>("embedtypes_golden.json");

/** A client carrying the given tags. */
function client(tags: string[]) {
  return clientMetadata("c", { tags: new Set(tags) });
}

describe("which clients a broadcast reaches", () => {
  it.each(golden.filters)("$name", (record) => {
    const filter = {
      requireAnyTag: record.require_any_tag ?? undefined,
      excludeTags: record.exclude_tags ?? undefined,
    };
    expect(filterMatches(filter, client(record.tags))).toBe(record.result);
  });

  it("lets an exclusion beat a requirement", () => {
    // The exclusion is the narrower statement: an operator adding one means
    // "not these", and a requirement that overrode it would make the
    // exclusion silently useless.
    expect(filterMatches({ requireAnyTag: ["a"], excludeTags: ["a"] }, client(["a"]))).toBe(false);
    expect(filterMatches({ requireAnyTag: ["a"], excludeTags: ["b"] }, client(["a", "b"]))).toBe(false);
  });

  it("treats an absent or empty list as no constraint", () => {
    // Attaching a filter to say one thing must not stop every broadcast.
    expect(filterMatches({}, client(["a"]))).toBe(true);
    expect(filterMatches({ requireAnyTag: [] }, client(["a"]))).toBe(true);
    expect(filterMatches({ excludeTags: [] }, client(["a"]))).toBe(true);
    expect(filterMatches({}, client([]))).toBe(true);
  });

  it("needs any one of the required tags, not all", () => {
    expect(filterMatches({ requireAnyTag: ["a", "b"] }, client(["b"]))).toBe(true);
    expect(filterMatches({ requireAnyTag: ["a", "b"] }, client(["c"]))).toBe(false);
  });

  it("consults a predicate last", () => {
    // After the tags, so a predicate is not asked about a client the tags
    // already ruled out.
    const seen: string[] = [];
    const filter = {
      excludeTags: ["a"],
      predicate: (meta: ReturnType<typeof client>) => {
        seen.push(meta.clientId);
        return true;
      },
    };
    expect(filterMatches(filter, client(["a"]))).toBe(false);
    expect(seen).toEqual([]);
    expect(filterMatches(filter, client(["b"]))).toBe(true);
    expect(seen).toEqual(["c"]);
  });

  it("lets a predicate refuse a client the tags allowed", () => {
    expect(filterMatches({ predicate: () => false }, client(["a"]))).toBe(false);
  });
});

describe("a client's defaults", () => {
  it("drops the oldest frame when it falls behind", () => {
    // Losing the start of a screen is better than losing the end, which is
    // what a viewer is actually looking at.
    expect(clientMetadata("c").backpressure).toBe(golden.default_backpressure.toLowerCase());
  });

  it("holds a bounded queue", () => {
    expect(clientMetadata("c").queueCapacity).toBe(golden.default_queue_capacity);
    expect(DEFAULT_QUEUE_CAPACITY).toBe(64);
  });

  it("keeps what it was given", () => {
    const meta = clientMetadata("c", { backpressure: "disconnect", queueCapacity: 1 });
    expect(meta.backpressure).toBe("disconnect");
    expect(meta.queueCapacity).toBe(1);
  });
});

describe("what an interceptor decided", () => {
  it("names every action the reference has", () => {
    expect([...INTERCEPT_ACTIONS]).toEqual(golden.intercept_actions.map((name) => name.toLowerCase()));
  });

  it("carries a payload only where there is one", () => {
    // A verdict that carries bytes and one that does not are different
    // things, and a caller reads the payload without asking which.
    expect(interceptPass()).toEqual({ action: "pass", payload: undefined });
    expect(interceptConsume()).toEqual({ action: "consume", payload: undefined });
    expect(interceptDefer()).toEqual({ action: "defer", payload: undefined });
    expect([...(interceptReplace(Uint8Array.from([120, 121])).payload as Uint8Array)]).toEqual([120, 121]);
    expect([...(interceptInject(Uint8Array.from([122])).payload as Uint8Array)]).toEqual([122]);
  });

  it("matches what the reference builds", () => {
    for (const [name, [action, payload]] of Object.entries(golden.intercept_defaults)) {
      const built =
        name === "replace"
          ? interceptReplace(Uint8Array.from([120, 121]))
          : name === "inject"
            ? interceptInject(Uint8Array.from([122]))
            : name === "consume"
              ? interceptConsume()
              : name === "defer"
                ? interceptDefer()
                : interceptPass();
      expect(built.action).toBe(action.toLowerCase());
      expect(built.payload === undefined ? null : [...built.payload]).toEqual(payload);
    }
  });
});

describe("answering a telnet negotiation", () => {
  it.each(golden.options)("$name", (record) => {
    expect([...onTelnetOption(record.command, record.option)]).toEqual(record.reply);
  });

  it("answers symmetrically", () => {
    // A DO is met with a WILL and a WILL with a DO: the policy accepts
    // whatever is offered.
    expect([...onTelnetOption(253, 24)]).toEqual([255, 251, 24]);
    expect([...onTelnetOption(251, 1)]).toEqual([255, 253, 1]);
  });

  it("mirrors a refusal", () => {
    // So neither end is left waiting on the other.
    expect([...onTelnetOption(252, 1)]).toEqual([255, 254, 1]);
    expect([...onTelnetOption(254, 24)]).toEqual([255, 252, 24]);
  });

  it("says nothing to a command it does not know", () => {
    expect([...onTelnetOption(200, 24)]).toEqual([]);
  });

  it("answers about the option it was asked about", () => {
    for (const option of [0, 1, 24, 255]) {
      expect([...onTelnetOption(253, option)][2]).toBe(option);
    }
  });
});

describe("answering a telnet subnegotiation", () => {
  it.each(golden.subnegotiations)("$name", (record) => {
    const body = Uint8Array.from(record.body);
    expect([...onTelnetSubnegotiation(DEFAULT_TELNET_POLICY, record.option, body)]).toEqual(record.reply);
    const wide = { terminalType: "xterm-256color", windowSize: [1000, 300] as const };
    expect([...onTelnetSubnegotiation(wide, record.option, body)]).toEqual(record.wide_reply);
  });

  it("answers a terminal-type request and not a statement", () => {
    // A server sending its own terminal type is telling, not asking.
    const request = Uint8Array.from([1]);
    expect(onTelnetSubnegotiation(DEFAULT_TELNET_POLICY, 24, request).length).toBeGreaterThan(0);
    expect([...onTelnetSubnegotiation(DEFAULT_TELNET_POLICY, 24, Uint8Array.from([0]))]).toEqual([]);
    expect([...onTelnetSubnegotiation(DEFAULT_TELNET_POLICY, 24, new Uint8Array(0))]).toEqual([]);
  });

  it("sends a window size in two bytes per dimension", () => {
    // A terminal wider than 255 columns is ordinary, and one byte would wrap
    // it.
    expect([...onTelnetSubnegotiation({ terminalType: "x", windowSize: [1000, 300] }, 31, new Uint8Array(0))]).toEqual(
      golden.wide_window,
    );
  });

  it("says nothing about an option nobody implemented", () => {
    // A wrong answer is worse than silence.
    expect([...onTelnetSubnegotiation(DEFAULT_TELNET_POLICY, 99, Uint8Array.from([1]))]).toEqual([]);
  });

  it("substitutes for a terminal type ASCII cannot carry", () => {
    // A question mark, which is what the reference writes on the way out —
    // not the replacement character, which is what decoding substitutes.
    expect([
      ...onTelnetSubnegotiation({ terminalType: "xterm-✓", windowSize: [80, 25] }, 24, Uint8Array.from([1])),
    ]).toEqual(golden.unencodable_terminal_type);
  });

  it("uses the defaults the reference does", () => {
    expect(DEFAULT_TELNET_POLICY.terminalType).toBe(golden.default_terminal_type);
    expect([...DEFAULT_TELNET_POLICY.windowSize]).toEqual(golden.default_window_size);
  });
});
