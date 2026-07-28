//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  CURRENT_PROTOCOL_VERSION,
  FRAME_TYPES,
  INPUT_MODES,
  MAX_PROTOCOL_VERSION,
  MIN_PROTOCOL_VERSION,
  negotiateProtocolVersion,
  PREFERRED_PROTOCOL_VERSION,
  SESSION_LIFECYCLES,
  VISIBILITIES,
} from "./index.ts";

interface ContractsGolden {
  min_protocol_version: number;
  max_protocol_version: number;
  preferred_protocol_version: number;
  current_protocol_version: number;
  negotiations: Array<{ client_min: number; client_max: number; selected: number | null }>;
  session_lifecycles: string[];
  input_modes: string[];
  visibilities: string[];
  frame_types: string[];
}

const golden = loadGolden<ContractsGolden>("contracts_golden.json");

describe("protocol version range", () => {
  it("matches the reference", () => {
    expect(MIN_PROTOCOL_VERSION).toBe(golden.min_protocol_version);
    expect(MAX_PROTOCOL_VERSION).toBe(golden.max_protocol_version);
    expect(PREFERRED_PROTOCOL_VERSION).toBe(golden.preferred_protocol_version);
  });

  it("aliases the current version to the preferred one", () => {
    // Outbound frames stamp this; if it drifted from what negotiation picks,
    // a peer would be told one version and sent another.
    expect(CURRENT_PROTOCOL_VERSION).toBe(golden.current_protocol_version);
    expect(CURRENT_PROTOCOL_VERSION).toBe(PREFERRED_PROTOCOL_VERSION);
  });
});

describe("negotiateProtocolVersion", () => {
  it.each(golden.negotiations)("client [$client_min, $client_max]", (record) => {
    // Both outcomes matter. Picking too low silently downgrades a pair that
    // could have spoken something newer; failing to refuse lets two peers
    // proceed disagreeing about the wire format, which shows up later as
    // corrupt frames instead of a clean 1002 close.
    expect(negotiateProtocolVersion(record.client_min, record.client_max)).toBe(record.selected ?? undefined);
  });

  it("takes the highest version both sides can speak", () => {
    // Not the client's preference and not the server's — the top of the
    // intersection. While MIN and MAX are the same version the intersection
    // is a single point, so this cannot yet distinguish highest from lowest;
    // it becomes a real assertion when the range widens.
    const wide = golden.negotiations.find((entry) => entry.client_min === -1 && entry.client_max === 3);
    expect(wide?.selected).toBe(MAX_PROTOCOL_VERSION);
    expect(MIN_PROTOCOL_VERSION).toBe(MAX_PROTOCOL_VERSION);
  });

  it("refuses a client that is entirely newer than the server", () => {
    const record = golden.negotiations.find((entry) => entry.client_min === 2 && entry.client_max === 3);
    expect(record?.selected).toBeNull();
    expect(negotiateProtocolVersion(2, 3)).toBeUndefined();
  });

  it("refuses a client that is entirely older than the server", () => {
    const record = golden.negotiations.find((entry) => entry.client_min === -1 && entry.client_max === 0);
    expect(record?.selected).toBeNull();
  });

  it("refuses a reversed range", () => {
    // A confused client can send max below min; the intersection is empty
    // and the handshake has to fail rather than pick something arbitrary.
    expect(negotiateProtocolVersion(3, 1)).toBeUndefined();
    expect(negotiateProtocolVersion(1, 0)).toBeUndefined();
  });

  it("accepts a range that only just touches the server's", () => {
    expect(negotiateProtocolVersion(MAX_PROTOCOL_VERSION, MAX_PROTOCOL_VERSION)).toBe(MAX_PROTOCOL_VERSION);
    expect(negotiateProtocolVersion(MIN_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION)).toBe(MIN_PROTOCOL_VERSION);
  });

  it("truncates a fractional bound toward zero", () => {
    // The reference coerces with int(); a client advertising 1.9 supports 1,
    // not 2.
    expect(negotiateProtocolVersion(1, 1.9)).toBe(1);
    expect(negotiateProtocolVersion(1.9, 3)).toBe(1);
  });
});

describe("contract literals", () => {
  it("lists the reference session lifecycles", () => {
    expect([...SESSION_LIFECYCLES]).toStrictEqual(golden.session_lifecycles);
  });

  it("lists the reference input modes", () => {
    expect([...INPUT_MODES]).toStrictEqual(golden.input_modes);
  });

  it("lists the reference visibilities", () => {
    expect([...VISIBILITIES]).toStrictEqual(golden.visibilities);
  });

  it("lists the reference frame types", () => {
    // These name the wire vocabulary shared with the Cloudflare adapter and
    // the browser; a missing one is a frame nobody can route.
    expect([...FRAME_TYPES]).toStrictEqual(golden.frame_types);
  });
});
