//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  MAX_PROTOCOL_VERSION,
  MIN_PROTOCOL_VERSION,
  negotiateProtocolVersion,
  readClientProtocolRange,
} from "./index.ts";

interface HelloGolden {
  server_min: number;
  server_max: number;
  hellos: Array<{
    name: string;
    hello: Record<string, unknown>;
    client_min: number;
    client_max: number;
    selected: number | null;
  }>;
}

const golden = loadGolden<HelloGolden>("workerhello_golden.json");

describe("reading a worker's protocol range", () => {
  it.each(golden.hellos)("$name", (record) => {
    const range = readClientProtocolRange(record.hello);
    expect(range).toEqual({ min: record.client_min, max: record.client_max });
    expect(negotiateProtocolVersion(range.min, range.max) ?? null).toBe(record.selected);
  });

  it("reads a worker that advertises nothing as version one", () => {
    // What every client did before the field existed. Refusing them would
    // disconnect every worker built against an older hub.
    expect(readClientProtocolRange({})).toEqual({ min: 1, max: 1 });
    expect(readClientProtocolRange({ input_mode: "open" })).toEqual({ min: 1, max: 1 });
  });

  it("reads a legacy version as a range of exactly itself", () => {
    // Not a minimum with an open top: a worker that can only speak version
    // one must not be handed version two because the hub supports it.
    expect(readClientProtocolRange({ protocol_version: 1 })).toEqual({ min: 1, max: 1 });
    expect(readClientProtocolRange({ protocol_version: 7 })).toEqual({ min: 7, max: 7 });
  });

  it("prefers the range object over a legacy version", () => {
    // The object is the only shape that can express a range at all, so a
    // worker sending both is a new client keeping an old field for
    // compatibility.
    expect(readClientProtocolRange({ protocol: { min: 1, max: 1 }, protocol_version: 9 })).toEqual({
      min: 1,
      max: 1,
    });
  });

  it("falls through to the legacy field when the range is not an object", () => {
    // A worker that sent a number where an object was expected has not sent a
    // range; whatever else it said still applies.
    expect(readClientProtocolRange({ protocol: 1, protocol_version: 1 })).toEqual({ min: 1, max: 1 });
    for (const protocol of [1, "1", [1, 1], null]) {
      expect(readClientProtocolRange({ protocol })).toEqual({ min: 1, max: 1 });
    }
  });

  it("never reads a version below one", () => {
    // Zero and negatives are not versions. Taking them literally would make
    // the negotiated floor lower than anything the hub ever spoke.
    for (const protocol of [
      { min: 0, max: 0 },
      { min: -3, max: -1 },
    ]) {
      expect(readClientProtocolRange({ protocol })).toEqual({ min: 1, max: 1 });
    }
    expect(readClientProtocolRange({ protocol_version: 0 })).toEqual({ min: 1, max: 1 });
    expect(readClientProtocolRange({ protocol_version: -2 })).toEqual({ min: 1, max: 1 });
  });

  it("truncates a fractional version rather than rounding it", () => {
    // A worker claiming 2.9 can speak two, not three.
    expect(readClientProtocolRange({ protocol: { min: 1.7, max: 2.9 } })).toEqual({ min: 1, max: 2 });
  });

  it("reads a version written as a string", () => {
    // Which a worker may do, and which changes the answer: two does not
    // overlap a hub that speaks only one.
    expect(readClientProtocolRange({ protocol_version: "2" })).toEqual({ min: 2, max: 2 });
    expect(negotiateProtocolVersion(2, 2)).toBeUndefined();
  });

  it("falls back rather than failing on a version it cannot read", () => {
    // The hello carries the input mode too; dropping the connection over an
    // unreadable version would lose that as well. The negotiation that
    // follows is what decides whether the two can talk.
    expect(readClientProtocolRange({ protocol_version: "nonsense" })).toEqual({ min: 1, max: 1 });
    expect(readClientProtocolRange({ protocol: { min: "nonsense", max: null } })).toEqual({ min: 1, max: 1 });
  });

  it("does not invent an overlap for a worker that is too new", () => {
    // The whole point of the negotiation: a range entirely above the hub has
    // no common version, and saying so is what closes the connection cleanly.
    const range = readClientProtocolRange({ protocol: { min: 5, max: 9 } });
    expect(range).toEqual({ min: 5, max: 9 });
    expect(negotiateProtocolVersion(range.min, range.max)).toBeUndefined();
  });

  it("leaves crossed bounds to the negotiation", () => {
    // Read as written rather than reordered: a worker sending max below min
    // has said something incoherent, and the negotiation is what refuses it.
    expect(readClientProtocolRange({ protocol: { min: 9, max: 1 } })).toEqual({ min: 9, max: 1 });
    expect(negotiateProtocolVersion(9, 1)).toBeUndefined();
  });

  it("fails the day the hub speaks a second version", () => {
    // While the hub's floor and ceiling coincide, several ways of getting the
    // range defaults wrong are indistinguishable: defaulting the minimum to
    // the ceiling, the maximum to the floor, or an absent hello to an open
    // range all produce the same answer as the correct code. This assertion
    // is what turns that from an invisible risk into a failing test at the
    // moment it becomes a real one.
    expect(MAX_PROTOCOL_VERSION).toBe(MIN_PROTOCOL_VERSION);
  });

  it("defaults each bound separately", () => {
    // A range naming only one end still has the other, taken from what the
    // hub itself speaks.
    expect(readClientProtocolRange({ protocol: { min: 1 } })).toEqual({ min: 1, max: MAX_PROTOCOL_VERSION });
    expect(readClientProtocolRange({ protocol: { max: 1 } })).toEqual({ min: MIN_PROTOCOL_VERSION, max: 1 });
    expect(golden.server_min).toBe(MIN_PROTOCOL_VERSION);
    expect(golden.server_max).toBe(MAX_PROTOCOL_VERSION);
  });
});
