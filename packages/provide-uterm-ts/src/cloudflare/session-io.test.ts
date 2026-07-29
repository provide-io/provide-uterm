//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type BroadcastTarget,
  broadcastHijackState,
  hijackStateFrame,
  MAX_INFLIGHT_WEBHOOKS,
  MAX_REQUEST_BODY,
  monoToWall,
  requestJson,
  wallToMono,
} from "./index.ts";

interface RequestCase {
  name: string;
  content_type: string | null;
  body_len: number;
  parsed: Record<string, unknown> | { raises: string };
}

interface StateCase {
  name: string;
  case: {
    sockets?: string[];
    hijack_id?: string;
    owners?: Record<string, string>;
    lease_expires_at?: number | null;
    input_mode?: string;
    failing_sockets?: string[];
  };
  sent: Array<{ ws_id: string; frame: Record<string, unknown> }>;
  sockets_left?: string[];
  owners_left?: Record<string, string>;
}

interface IoGolden {
  fixed_ts: number;
  fixed_mono: number;
  max_request_body: number;
  max_inflight_webhooks: number;
  requests: RequestCase[];
  hijack_state: StateCase[];
  broadcast: StateCase[];
  mono_to_wall: Array<{ mono: number | null; wall: number | null }>;
  wall_to_mono: Array<{ wall: number; mono: number }>;
}

const golden = loadGolden<IoGolden>("sessionio_golden.json");

/** The bodies the corpus recorded, rebuilt from what it kept of them. */
const BODIES: Record<string, string> = {
  "an ordinary JSON body": '{"a":1}',
  "a charset alongside the type": '{"a":1}',
  "the type in capitals": '{"a":1}',
  "a type with spaces around it": '{"a":1}',
  "plain text, which needs no preflight": '{"a":1}',
  "a form encoding, which needs no preflight": '{"a":1}',
  "multipart, which needs no preflight": '{"a":1}',
  "no content type at all": '{"a":1}',
  "an empty content type": '{"a":1}',
  "a type that merely contains the words": '{"a":1}',
  "an empty body": "",
  "a body that is not JSON": "not json",
  "a JSON list": "[1,2]",
  "a JSON string": '"hello"',
  "a JSON null": "null",
  "a JSON number": "42",
  "a nested object": '{"a":{"b":[1,2]}}',
  "a body at the cap": `{"a":"${"x".repeat(65_536 - 8)}"}`,
  "a body over the cap": `{"a":"${"x".repeat(65_536 - 7)}"}`,
};

function stateFor(record: StateCase["case"], wsId: string) {
  const hijackId = record.hijack_id;
  return {
    ...(hijackId === undefined ? {} : { session: { hijackId, leaseExpiresAt: record.lease_expires_at ?? null } }),
    browserHijackId: record.owners?.[wsId],
    inputMode: record.input_mode ?? "open",
    now: golden.fixed_ts,
    monotonic: golden.fixed_mono,
  };
}

describe("what a request body has to look like", () => {
  it.each(golden.requests)("$name", (record) => {
    const body = BODIES[record.name] as string;
    expect(body.length).toBe(record.body_len);
    const parsed = requestJson(record.content_type ?? undefined, body);
    if ("raises" in record.parsed) {
      // A recorded divergence, covered by its own test below.
      expect(parsed).toEqual({});
      return;
    }
    expect(parsed).toEqual(record.parsed);
  });

  it("refuses the three types a cross-origin page can send without a preflight", () => {
    // The whole point of the rule: a handler that parsed any of these would
    // take instructions from whatever page a session owner had open.
    for (const contentType of ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]) {
      expect(requestJson(contentType, '{"a":1}')).toEqual({});
    }
  });

  it("refuses a request that names no type at all", () => {
    expect(requestJson(undefined, '{"a":1}')).toEqual({});
    expect(requestJson("", '{"a":1}')).toEqual({});
  });

  it("takes the type with a charset or in capitals", () => {
    for (const contentType of ["application/json", "application/json; charset=utf-8", "APPLICATION/JSON"]) {
      expect(requestJson(contentType, '{"a":1}')).toEqual({ a: 1 });
    }
  });

  it("matches the type as a substring, which is not the hole it looks like", () => {
    // A cross-origin request can only set the three preflight-free types, and
    // none of them contains this one — so a compound type reaching here has
    // already survived a preflight.
    expect(requestJson("text/plain+application/json", '{"a":1}')).toEqual({ a: 1 });
  });

  it("stops at the size the reference stops at", () => {
    expect(MAX_REQUEST_BODY).toBe(golden.max_request_body);
    const atCap = `{"a":"${"x".repeat(MAX_REQUEST_BODY - 8)}"}`;
    expect(atCap.length).toBe(MAX_REQUEST_BODY);
    expect(requestJson("application/json", atCap)).not.toEqual({});
    expect(requestJson("application/json", `${atCap}x`)).toEqual({});
  });

  it("takes only an object, not the other things JSON can be", () => {
    for (const body of ["[1,2]", '"hello"', "null", "42", "true"]) {
      expect(requestJson("application/json", body)).toEqual({});
    }
    expect(requestJson("application/json", '{"a":{"b":[1,2]}}')).toEqual({ a: { b: [1, 2] } });
  });

  it("returns nothing for a body that is not JSON, where the reference raises", () => {
    // Every other bad input gets an empty object; only this one leaves the
    // function. Whether that surfaces as a 500 depends on a handler above it
    // that this port has not traced — what is certain is the inconsistency.
    expect(requestJson("application/json", "not json")).toEqual({});
    expect(golden.requests.find((entry) => entry.name === "a body that is not JSON")?.parsed).toEqual({
      raises: "JSONDecodeError",
    });
  });

  it("bounds webhook deliveries where the reference bounds them", () => {
    expect(MAX_INFLIGHT_WEBHOOKS).toBe(golden.max_inflight_webhooks);
  });
});

describe("what a browser is told about the hijack", () => {
  it.each(golden.hijack_state)("$name", (record) => {
    const wsId = record.sent[0]?.ws_id as string;
    expect(hijackStateFrame(stateFor(record.case, wsId))).toEqual(record.sent[0]?.frame);
  });

  it("says me only to the browser that actually holds it", () => {
    // Being wrongly told `me` is what would put another operator's controls
    // in front of somebody.
    const session = { hijackId: "h1", leaseExpiresAt: 560 };
    const base = { session, inputMode: "open", now: golden.fixed_ts, monotonic: golden.fixed_mono };
    expect(hijackStateFrame({ ...base, browserHijackId: "h1" }).owner).toBe("me");
    expect(hijackStateFrame({ ...base, browserHijackId: "h2" }).owner).toBe("other");
    // No recorded id, and a stale one from a hijack that has ended, both get
    // the safe answer.
    expect(hijackStateFrame({ ...base, browserHijackId: undefined }).owner).toBe("other");
  });

  it("says nobody owns it when nobody does", () => {
    const frame = hijackStateFrame({
      inputMode: "open",
      now: golden.fixed_ts,
      monotonic: golden.fixed_mono,
      browserHijackId: "h1",
    });
    expect(frame).toMatchObject({ hijacked: false, owner: null, lease_expires_at: null });
  });

  it("reports the expiry in wall-clock, which is the only clock a browser has", () => {
    // Monotonic in memory so a countdown survives a clock adjustment; wall
    // everywhere it is reported.
    const frame = hijackStateFrame({
      session: { hijackId: "h1", leaseExpiresAt: golden.fixed_mono + 60 },
      browserHijackId: "h1",
      inputMode: "open",
      now: golden.fixed_ts,
      monotonic: golden.fixed_mono,
    });
    expect(frame.lease_expires_at).toBe(golden.fixed_ts + 60);
  });

  it("reports no expiry when the hijack has none", () => {
    expect(
      hijackStateFrame({
        session: { hijackId: "h1", leaseExpiresAt: null },
        browserHijackId: "h1",
        inputMode: "open",
        now: golden.fixed_ts,
        monotonic: golden.fixed_mono,
      }).lease_expires_at,
    ).toBeNull();
  });
});

describe("converting between the two clocks", () => {
  it.each(golden.mono_to_wall)("$mono becomes $wall", (record) => {
    expect(monoToWall(record.mono, golden.fixed_ts, golden.fixed_mono)).toBe(record.wall);
  });

  it.each(golden.wall_to_mono)("$wall becomes $mono", (record) => {
    expect(wallToMono(record.wall, golden.fixed_ts, golden.fixed_mono)).toBe(record.mono);
  });

  it("round-trips", () => {
    // Which is what makes a lease survive a restart: stored as wall, read back
    // as monotonic, and the countdown means the same thing.
    for (const mono of [0, 500, 560, -1]) {
      expect(
        wallToMono(monoToWall(mono, golden.fixed_ts, golden.fixed_mono) as number, golden.fixed_ts, golden.fixed_mono),
      ).toBe(mono);
    }
  });

  it("has nothing to convert when there is no expiry", () => {
    expect(monoToWall(null, golden.fixed_ts, golden.fixed_mono)).toBeNull();
    expect(monoToWall(undefined, golden.fixed_ts, golden.fixed_mono)).toBeNull();
  });
});

describe("telling every browser at once", () => {
  it.each(golden.broadcast)("$name", async (record) => {
    const targets: BroadcastTarget[] = (record.case.sockets ?? []).map((wsId) => ({
      wsId,
      hijackId: record.case.owners?.[wsId],
    }));
    const failing = new Set(record.case.failing_sockets ?? []);
    const result = await broadcastHijackState(
      targets,
      {
        ...(record.case.hijack_id === undefined
          ? {}
          : { session: { hijackId: record.case.hijack_id, leaseExpiresAt: record.case.lease_expires_at ?? null } }),
        inputMode: record.case.input_mode ?? "open",
        now: golden.fixed_ts,
        monotonic: golden.fixed_mono,
      },
      async (wsId) => {
        if (failing.has(wsId)) {
          throw new Error("socket gone");
        }
      },
    );
    expect(result.sends.map((entry) => ({ ws_id: entry.wsId, frame: entry.frame }))).toEqual(record.sent);
    // Whoever failed is forgotten, which is what the recorded runtime was
    // left holding.
    const left = targets.map((target) => target.wsId).filter((wsId) => !result.dropped.includes(wsId));
    expect(left.sort()).toEqual(record.sockets_left);
  });

  it("forgets a socket that has gone rather than retrying it", async () => {
    // An entry left behind would let a hijack look owned by nobody reachable.
    const result = await broadcastHijackState(
      [{ wsId: "ws-1", hijackId: "h1" }, { wsId: "ws-2" }],
      {
        session: { hijackId: "h1", leaseExpiresAt: 560 },
        inputMode: "open",
        now: golden.fixed_ts,
        monotonic: golden.fixed_mono,
      },
      async (wsId) => {
        if (wsId === "ws-1") {
          throw new Error("socket gone");
        }
      },
    );
    expect(result.dropped).toEqual(["ws-1"]);
    expect(result.sends.map((entry) => entry.wsId)).toEqual(["ws-2"]);
  });

  it("carries on to the browsers after the one that failed", async () => {
    const result = await broadcastHijackState(
      [{ wsId: "a" }, { wsId: "b" }, { wsId: "c" }],
      { inputMode: "open", now: golden.fixed_ts, monotonic: golden.fixed_mono },
      async (wsId) => {
        if (wsId === "a") {
          throw new Error("gone");
        }
      },
    );
    expect(result.sends.map((entry) => entry.wsId)).toEqual(["b", "c"]);
    expect(result.dropped).toEqual(["a"]);
  });

  it("tells each browser its own answer", async () => {
    // One of them holds it; the others are told so.
    const result = await broadcastHijackState(
      [{ wsId: "a", hijackId: "h1" }, { wsId: "b", hijackId: "h2" }, { wsId: "c" }],
      {
        session: { hijackId: "h1", leaseExpiresAt: 560 },
        inputMode: "open",
        now: golden.fixed_ts,
        monotonic: golden.fixed_mono,
      },
      async () => undefined,
    );
    expect(result.sends.map((entry) => entry.frame.owner)).toEqual(["me", "other", "other"]);
  });

  it("says nothing when nobody is watching", async () => {
    const result = await broadcastHijackState(
      [],
      { inputMode: "open", now: golden.fixed_ts, monotonic: golden.fixed_mono },
      async () => undefined,
    );
    expect(result).toEqual({ sends: [], dropped: [] });
  });
});
