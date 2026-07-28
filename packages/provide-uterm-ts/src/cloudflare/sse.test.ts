//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { buildSseResponse, routeSse, SSE_MAX_EVENTS, SSE_RETRY_MS, type SseRuntime } from "./index.ts";

interface SseGolden {
  retry_ms: number;
  max_events: number;
  worker_id: string;
  status: number;
  headers: Record<string, string>;
  custom_retry_body: string;
  bodies: Array<{ name: string; events: Array<Record<string, unknown>>; body: string }>;
  positions: Array<{
    name: string;
    url: string;
    headers: Record<string, string> | null;
    asked: { worker_id: string; after_seq: number; limit: number };
  }>;
  whole_float_event: Record<string, unknown>;
  whole_float_body: string;
  beyond_exact_url: string;
  beyond_exact_after_seq: string;
  wrong_session: { status: number; body: string; headers: Record<string, string>; store_calls: number };
  matching_session: { status: number; body: string; asked: [string, number, number] };
}

const golden = loadGolden<SseGolden>("cfsse_golden.json");

/** A store that records what it was asked for. */
class RecordingStore {
  readonly calls: Array<[string, number, number]> = [];
  readonly events: Array<Record<string, unknown>>;

  constructor(events: Array<Record<string, unknown>> = []) {
    this.events = events;
  }

  listEventsSince(workerId: string, afterSeq: number, limit: number): Array<Record<string, unknown>> {
    this.calls.push([workerId, afterSeq, limit]);
    return this.events;
  }
}

/** The smallest runtime the route reads. */
function runtime(store: RecordingStore, workerId: string = golden.worker_id): SseRuntime {
  return { workerId, store };
}

/** A request carrying headers, or not carrying them at all. */
function request(headers: Record<string, string> | null): { headers?: Headers } {
  // A real `Headers`, so the case-insensitive lookup a browser's
  // `Last-Event-ID` relies on is exercised rather than assumed.
  return headers === null ? {} : { headers: new Headers(headers) };
}

/** What the route asked the store for. */
async function asked(url: string, headers: Record<string, string> | null = null): Promise<[string, number, number]> {
  const store = new RecordingStore();
  await routeSse(runtime(store), request(headers), url, golden.worker_id);
  return store.calls[0] as [string, number, number];
}

describe("the response body", () => {
  it.each(golden.bodies)("$name", async (record) => {
    expect(await buildSseResponse(record.events).text()).toBe(record.body);
  });

  it("writes an id line the client sends back", async () => {
    // `EventSource` echoes the last id in `Last-Event-ID`, which is the only
    // way a reconnecting client says where it got to.
    const body = await buildSseResponse([{ seq: 42 }]).text();
    expect(body).toContain("id: 42\n");
  });

  it("separates events with a blank line", async () => {
    // Without it the two `data:` lines would be read as one event.
    const body = await buildSseResponse([{ seq: 1 }, { seq: 2 }]).text();
    expect(body).toContain("\n\nid: 2\n");
  });

  it("tells the client when to come back", async () => {
    // The connection closes after every batch, so without this the browser
    // uses its own default and the session stalls for as long as that is.
    expect(await buildSseResponse([]).text()).toBe(`retry: ${SSE_RETRY_MS}\n`);
    expect(SSE_RETRY_MS).toBe(golden.retry_ms);
  });

  it("accepts a different interval", async () => {
    expect(await buildSseResponse([], { retryMs: 500 }).text()).toBe(golden.custom_retry_body);
  });

  it("keeps a newline in the payload from splitting the event", async () => {
    // SSE is line-oriented, so a raw newline in the data would terminate the
    // event early and the rest would be read as a new field.
    const body = await buildSseResponse([{ seq: 1, data: "a\nb" }]).text();
    expect(body).toBe(golden.bodies.find((entry) => entry.name === "a payload containing a newline")?.body);
    expect(body.split("\n").filter((line) => line.startsWith("data: "))).toHaveLength(1);
  });

  it("escapes non-ascii the way the reference does", async () => {
    // Both ends read the field as JSON, so this is not a correctness
    // requirement — it is a byte-for-byte one, and the two encoders disagree
    // by default.
    const body = await buildSseResponse([{ seq: 2, data: "héllo → ✓" }]).text();
    expect(body).toContain("h\\u00e9llo");
    expect(body).not.toContain("héllo");
  });

  it("renders the fields in the order they were written", async () => {
    // The reference does not sort them, and sorting here would change every
    // byte of every event.
    const body = await buildSseResponse([{ seq: 1, zeta: 1, alpha: 2 }]).text();
    expect(body).toContain('{"seq": 1, "zeta": 1, "alpha": 2}');
  });

  it("still writes an id for an event that has no sequence", async () => {
    // Empty rather than absent: the client's position simply does not
    // advance, which is why the store always writes one.
    expect(await buildSseResponse([{ type: "tick" }]).text()).toContain("id: \n");
  });
});

describe("the response itself", () => {
  it("is a stream the browser will hold open", async () => {
    const response = buildSseResponse([]);
    expect(response.status).toBe(golden.status);
    for (const [name, value] of Object.entries(golden.headers)) {
      expect(response.headers.get(name)).toBe(value);
    }
  });

  it("tells the proxies in between not to buffer it", () => {
    // A proxy holding the batch back would defeat the point of sending it.
    expect(golden.headers["x-accel-buffering"]).toBe("no");
    expect(golden.headers["cache-control"]).toBe("no-cache");
  });
});

describe("where the client says it got to", () => {
  it.each(golden.positions)("$name", async (record) => {
    const [workerId, afterSeq, limit] = await asked(record.url, record.headers);
    expect(workerId).toBe(record.asked.worker_id);
    expect(afterSeq).toBe(record.asked.after_seq);
    expect(limit).toBe(record.asked.limit);
  });

  it("reads the query parameter", async () => {
    expect((await asked("https://h/s?after_seq=41"))[1]).toBe(41);
  });

  it("reads the reconnect header when there is no parameter", async () => {
    expect((await asked("https://h/s", { "Last-Event-ID": "17" }))[1]).toBe(17);
  });

  it("prefers the parameter over the header", async () => {
    // The parameter is what this request asked for; the header is only where
    // the last one ended.
    expect((await asked("https://h/s?after_seq=5", { "last-event-id": "17" }))[1]).toBe(5);
  });

  it("starts from the beginning when there is neither", async () => {
    // Not from wherever the session happens to be — a client with no
    // position has seen nothing, and starting later would silently skip it.
    expect((await asked("https://h/s"))[1]).toBe(0);
    expect((await asked("https://h/s", {}))[1]).toBe(0);
  });

  it("starts from the beginning when the position cannot be read", async () => {
    // Replaying events a client has already seen is recoverable — they carry
    // their own sequence numbers — while refusing the request would leave it
    // with no stream at all.
    for (const raw of ["abc", "1.5", "", "0x10", "nan"]) {
      expect((await asked(`https://h/s?after_seq=${raw}`))[1]).toBe(0);
    }
  });

  it("clamps a negative position rather than passing it through", async () => {
    // The store's `seq > ?` would accept it and return everything, which is
    // the same answer — but only by accident of the comparison.
    expect((await asked("https://h/s?after_seq=-5"))[1]).toBe(0);
    expect((await asked("https://h/s", { "last-event-id": "-9" }))[1]).toBe(0);
  });

  it("stops the query at a fragment", async () => {
    expect((await asked("https://h/s?after_seq=4#after_seq=99"))[1]).toBe(4);
  });

  it("reads nothing from a query inside a fragment", async () => {
    // Everything after the marker belongs to the fragment, so the query never
    // starts. Looking for the marker only after the question mark would read
    // this as a position.
    expect((await asked("https://h/s#frag?after_seq=9"))[1]).toBe(0);
  });

  it("does not match a parameter whose name merely starts the same", async () => {
    expect((await asked("https://h/s?after_seqx=5&after_seq=9"))[1]).toBe(9);
  });

  it("reads a plus as a space", async () => {
    // Form encoding, which the reference decodes before parsing. Left as a
    // plus this is not a trailing sign and the position would be unreadable.
    expect((await asked("https://h/s?after_seq=7+"))[1]).toBe(7);
  });

  it("begins the query at the first question mark", async () => {
    // A second one is part of a value. Splitting at the last would read the
    // tail as the whole query and find a position that was never sent.
    expect((await asked("https://h/s?a=1?after_seq=9"))[1]).toBe(0);
  });

  it("splits a pair at its first equals sign", async () => {
    // This is the parameter carrying an unreadable value, not some other
    // parameter — so the second occurrence does not get a turn.
    expect((await asked("https://h/s?after_seq=5=6&after_seq=9"))[1]).toBe(0);
  });

  it("drops a pair with no value and keeps looking", async () => {
    // Reading a bare name as a name one character shorter would match this
    // one and stop here, on a value that is not a position.
    expect((await asked("https://h/s?after_seqs&after_seq=9"))[1]).toBe(9);
  });

  it("takes the first of a repeated parameter", async () => {
    expect((await asked("https://h/s?after_seq=3&after_seq=9"))[1]).toBe(3);
  });

  it("finds the parameter among others", async () => {
    expect((await asked("https://h/s?foo=bar&after_seq=8&baz=1"))[1]).toBe(8);
  });

  it("reads a relative url", async () => {
    // The route is handed whatever the caller had, which is not always
    // absolute — and `new URL` on its own would throw on this.
    expect((await asked("/api/sessions/x/events/stream?after_seq=6"))[1]).toBe(6);
  });

  it("reads a url with no query at all", async () => {
    expect((await asked("https://h/s"))[1]).toBe(0);
    expect((await asked("https://h/s?"))[1]).toBe(0);
  });

  it("bounds the batch", async () => {
    // A client returning after a long absence must not be handed the whole
    // log in one response; it takes what fits and reconnects for the rest.
    expect((await asked("https://h/s"))[2]).toBe(SSE_MAX_EVENTS);
    expect(SSE_MAX_EVENTS).toBe(golden.max_events);
  });

  it("asks for the session it is, not the one in the path", async () => {
    // The path is checked separately; what the store is asked for is the
    // object's own session, so a mismatch cannot read another session's log.
    const store = new RecordingStore();
    await routeSse(runtime(store, "w-mine"), request(null), "https://h/s", "w-mine");
    expect(store.calls[0]?.[0]).toBe("w-mine");
  });
});

describe("a request for another session", () => {
  it("is refused", async () => {
    const store = new RecordingStore();
    const response = await routeSse(runtime(store), request(null), "https://h/s", "somebody-else");
    expect(response.status).toBe(golden.wrong_session.status);
    expect(await response.text()).toBe(golden.wrong_session.body);
    expect(response.headers.get("content-type")).toBe(golden.wrong_session.headers["content-type"]);
  });

  it("does not read the log first", async () => {
    // The refusal is the point: reaching the store at all would mean a
    // mismatched id had already been used to read something.
    const store = new RecordingStore([{ seq: 1 }]);
    await routeSse(runtime(store), request(null), "https://h/s", "somebody-else");
    expect(store.calls).toHaveLength(golden.wrong_session.store_calls);
    expect(store.calls).toHaveLength(0);
  });

  it("is refused as json, not as a stream", async () => {
    // A browser's `EventSource` would otherwise try to parse the refusal as
    // events.
    const response = await routeSse(runtime(new RecordingStore()), request(null), "/s", "no");
    expect(response.headers.get("content-type")).toBe("application/json");
  });
});

describe("a request for this session", () => {
  it("returns the events since the position", async () => {
    const store = new RecordingStore(golden.bodies[0]?.events ?? []);
    const response = await routeSse(runtime(store), request(null), "https://h/s?after_seq=41", golden.worker_id);
    expect(response.status).toBe(golden.matching_session.status);
    expect(await response.text()).toBe(golden.matching_session.body);
    expect(store.calls[0]).toEqual(golden.matching_session.asked);
  });
});

describe("known divergences from the reference", () => {
  it("renders a whole-valued timestamp without a decimal point", async () => {
    // Python renders `1700000000.0` because the value is a float; ECMAScript
    // has no such type. Pinned rather than left to be found in a client that
    // reads the field back — the two bodies differ by two bytes.
    const body = await buildSseResponse([golden.whole_float_event]).text();
    expect(golden.whole_float_body).toContain('"ts": 1700000000.0');
    expect(body).toContain('"ts": 1700000000');
    expect(body).not.toBe(golden.whole_float_body);
  });

  it("rounds a position beyond the exactly-representable range", async () => {
    // Python keeps the integer exactly; ECMAScript rounds it to the nearest
    // double. Both then ask for events after a sequence far larger than any
    // that exists, so the answer is the same — but the number asked for is
    // not.
    const [, afterSeq] = await asked(golden.beyond_exact_url);
    expect(String(afterSeq)).not.toBe(golden.beyond_exact_after_seq);
    expect(afterSeq).toBeGreaterThan(Number.MAX_SAFE_INTEGER);
  });
});
