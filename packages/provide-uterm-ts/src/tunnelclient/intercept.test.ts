//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  DENYLISTED_HEADERS,
  type InterceptDecision,
  InterceptGate,
  MIN_TIMEOUT_S,
  parseActionMessage,
  sanitizeHeaders,
} from "./index.ts";

interface InterceptGolden {
  denylist: string[];
  parsed: Array<{
    name: string;
    message: Record<string, unknown>;
    decision: { action: string; headers: Record<string, string> | null; body: string | null };
  }>;
  gates: Array<{
    name: string;
    given: { timeout_s: number; timeout_action: string };
    enabled: boolean;
    inspect_enabled: boolean;
    timeout_s: number;
    timeout_action: string;
    pending_count: number;
  }>;
  flows: Array<{
    name: string;
    pending_while_waiting: number;
    resolved: boolean;
    decision: { action: string; headers: Record<string, string> | null; body: string | null } | null;
    pending_after: number;
  }>;
}

const golden = loadGolden<InterceptGolden>("intercept_golden.json");

/** A decision in the shape the corpus records. */
function recorded(decision: InterceptDecision) {
  return {
    action: decision.action,
    headers: decision.headers ?? null,
    body: decision.body === undefined ? null : Buffer.from(decision.body).toString("base64"),
  };
}

/** A wait that never finishes, for a test about what arrives first. */
const never = () => new Promise<void>(() => {});

describe("what an operator may not rewrite", () => {
  it("denies exactly what the reference denies", () => {
    expect([...DENYLISTED_HEADERS].sort()).toEqual(golden.denylist);
  });

  it("denies the headers that make request smuggling possible", () => {
    // A length or an encoding the operator chose is how a downstream server is
    // made to read one request as two.
    for (const header of ["content-length", "transfer-encoding"]) {
      expect(DENYLISTED_HEADERS.has(header)).toBe(true);
    }
  });

  it("denies the headers that would let an operator become somebody else", () => {
    // Forwarding these is impersonating the original requester, which is the
    // thing interception exists to make visible.
    for (const header of ["host", "authorization", "cookie", "x-forwarded-for", "x-real-ip"]) {
      expect(DENYLISTED_HEADERS.has(header)).toBe(true);
    }
  });

  it("denies hop-by-hop headers, which are the connection's own", () => {
    for (const header of ["connection", "keep-alive", "te", "trailer", "upgrade", "proxy-authorization"]) {
      expect(DENYLISTED_HEADERS.has(header)).toBe(true);
    }
  });

  it("matches a denied header however it is capitalised", () => {
    // `AUTHORIZATION` is the same header, and a case-sensitive check would be
    // no check at all.
    for (const name of ["Authorization", "AUTHORIZATION", "aUtHoRiZaTiOn", "Host", "X-Forwarded-For"]) {
      expect(sanitizeHeaders({ [name]: "x" })).toEqual({});
    }
  });

  it("keeps everything it is not denying, with its case", () => {
    expect(sanitizeHeaders({ "X-Trace": "1", Accept: "text/plain" })).toEqual({
      "X-Trace": "1",
      Accept: "text/plain",
    });
  });

  it("keeps the allowed headers when only some are denied", () => {
    expect(sanitizeHeaders({ "X-Trace": "1", Authorization: "Bearer stolen" })).toEqual({ "X-Trace": "1" });
  });

  it("does not change what it was given", () => {
    // The caller may still need to show the operator what they asked for.
    const raw = { Host: "evil.example", "X-Trace": "1" };
    sanitizeHeaders(raw);
    expect(raw).toEqual({ Host: "evil.example", "X-Trace": "1" });
  });

  it("says which headers it dropped, so an edit that did not take is visible", () => {
    const dropped: string[][] = [];
    sanitizeHeaders(
      { Host: "evil", Authorization: "Bearer x", "X-Trace": "1" },
      { headersDenied: (names) => dropped.push([...names]), invalidBody: () => {} },
    );
    expect(dropped).toEqual([["Authorization", "Host"]]);
  });

  it("says nothing when it dropped nothing", () => {
    let called = 0;
    sanitizeHeaders({ "X-Trace": "1" }, { headersDenied: () => (called += 1), invalidBody: () => {} });
    expect(called).toBe(0);
  });
});

describe("reading a decision", () => {
  it.each(golden.parsed)("$name", (record) => {
    expect(recorded(parseActionMessage(record.message))).toEqual(record.decision);
  });

  it("treats anything it does not understand as forwarding", () => {
    // Which keeps traffic moving. Dropping on a message it could not read
    // would make a typo look like an outage.
    for (const action of ["sideways", "FORWARD", "", 42, null, undefined, {}]) {
      expect(parseActionMessage({ action }).action).toBe("forward");
    }
    expect(parseActionMessage({}).action).toBe("forward");
  });

  it("takes the three actions it does understand", () => {
    for (const action of ["forward", "drop", "modify"] as const) {
      expect(parseActionMessage({ action }).action).toBe(action);
    }
  });

  it("ignores headers and a body on a decision that is not a rewrite", () => {
    // They mean nothing there, and reading them would be reading input nobody
    // is going to use.
    for (const action of ["forward", "drop"]) {
      const decision = parseActionMessage({
        action,
        headers: { "X-Trace": "1" },
        body_b64: Buffer.from("hello").toString("base64"),
      });
      expect(decision.headers).toBeUndefined();
      expect(decision.body).toBeUndefined();
    }
  });

  it("takes headers only when they are a mapping", () => {
    expect(parseActionMessage({ action: "modify", headers: ["Host: evil"] }).headers).toBeUndefined();
    expect(parseActionMessage({ action: "modify", headers: "Host: evil" }).headers).toBeUndefined();
    expect(parseActionMessage({ action: "modify", headers: null }).headers).toBeUndefined();
    expect(parseActionMessage({ action: "modify" }).headers).toBeUndefined();
  });

  it("stringifies header values, as the reference does", () => {
    expect(parseActionMessage({ action: "modify", headers: { "X-Count": 5 } }).headers).toEqual({ "X-Count": "5" });
  });

  it("reads a body only when it is really base64", () => {
    expect(parseActionMessage({ action: "modify", body_b64: Buffer.from("hello").toString("base64") }).body).toEqual(
      new TextEncoder().encode("hello"),
    );
    expect(parseActionMessage({ action: "modify", body_b64: "" }).body).toEqual(new Uint8Array());
  });

  it("refuses a body it would have to guess at", () => {
    // Including one whose padding is missing: a body decoded from something
    // the sender did not mean is a body nobody asked for.
    for (const body of ["not base64!!", "aGVsbG8", "a", "====", "aGVsbG8=extra"]) {
      expect(parseActionMessage({ action: "modify", body_b64: body }).body).toBeUndefined();
    }
  });

  it("says when it could not read a body, naming the request", () => {
    const seen: unknown[] = [];
    parseActionMessage(
      { action: "modify", body_b64: "not base64!!", id: "r1" },
      { headersDenied: () => {}, invalidBody: (id) => seen.push(id) },
    );
    expect(seen).toEqual(["r1"]);
  });

  it("says nothing about a body that was fine", () => {
    let called = 0;
    parseActionMessage(
      { action: "modify", body_b64: "aGVsbG8=" },
      { headersDenied: () => {}, invalidBody: () => (called += 1) },
    );
    expect(called).toBe(0);
  });

  it("takes no body at all as no body, quietly", () => {
    // A message that carried no body did not fail to carry one, so there is
    // nothing to report — a warning here would cry wolf on every forward.
    let complaints = 0;
    const logger = { headersDenied: () => {}, invalidBody: () => (complaints += 1) };
    for (const message of [
      { action: "modify" },
      { action: "modify", body_b64: 42 },
      { action: "modify", body_b64: null },
      { action: "modify", body_b64: ["aGk="] },
    ]) {
      expect(parseActionMessage(message, logger).body).toBeUndefined();
    }
    expect(complaints).toBe(0);
  });
});

describe("how a gate is configured", () => {
  it.each(golden.gates)("$name", (record) => {
    const gate = new InterceptGate(record.given.timeout_s, record.given.timeout_action);
    expect({
      enabled: gate.enabled,
      inspect_enabled: gate.inspectEnabled,
      timeout_s: gate.timeoutS,
      timeout_action: gate.timeoutAction,
      pending_count: gate.pendingCount,
    }).toEqual({
      enabled: record.enabled,
      inspect_enabled: record.inspect_enabled,
      timeout_s: record.timeout_s,
      timeout_action: record.timeout_action,
      pending_count: record.pending_count,
    });
  });

  it("starts paused-off and visible", () => {
    // Interception is opt-in; inspection is not, because seeing traffic is
    // the point of a tunnel client.
    const gate = new InterceptGate();
    expect(gate.enabled).toBe(false);
    expect(gate.inspectEnabled).toBe(true);
  });

  it("refuses to be configured to release everything instantly", () => {
    for (const given of [0, 0.1, -5, Number.NEGATIVE_INFINITY]) {
      expect(new InterceptGate(given).timeoutS).toBe(MIN_TIMEOUT_S);
    }
    expect(new InterceptGate(45).timeoutS).toBe(45);
  });

  it("falls back to forwarding for a timeout action nobody defined", () => {
    for (const action of ["sideways", "modify", "", "DROP"]) {
      expect(new InterceptGate(5, action).timeoutAction).toBe("forward");
    }
    expect(new InterceptGate(5, "drop").timeoutAction).toBe("drop");
  });
});

describe("a request waiting on somebody", () => {
  it.each(golden.flows)("$name", async (record) => {
    // Each flow is replayed by name, since what each one drives differs.
    const gate = new InterceptGate(5, record.name === "nothing arriving before the timeout" ? "drop" : "forward");
    if (record.name === "a decision arriving") {
      const waiting = gate.awaitDecision("r1", never);
      expect(gate.pendingCount).toBe(record.pending_while_waiting);
      expect(gate.resolve("r1", parseActionMessage({ action: "drop" }))).toBe(record.resolved);
      expect(recorded(await waiting)).toEqual(record.decision);
    } else if (record.name === "a decision nobody was waiting for") {
      expect(gate.resolve("r-unknown", parseActionMessage({ action: "drop" }))).toBe(record.resolved);
    } else if (record.name === "a second decision for the same request") {
      const waiting = gate.awaitDecision("r1", never);
      const first = gate.resolve("r1", parseActionMessage({ action: "drop" }));
      const second = gate.resolve("r1", parseActionMessage({ action: "forward" }));
      expect(first && !second).toBe(record.resolved);
      expect(recorded(await waiting)).toEqual(record.decision);
    } else if (record.name === "nothing arriving before the timeout") {
      expect(recorded(await gate.awaitDecision("r1", async () => {}))).toEqual(record.decision);
    } else if (record.name === "everything released at once") {
      const waits = [0, 1, 2].map((index) => gate.awaitDecision(`r${index}`, never));
      expect(gate.pendingCount).toBe(record.pending_while_waiting);
      expect(gate.cancelAll("drop") === 3).toBe(record.resolved);
      expect(recorded(await (waits[0] as Promise<InterceptDecision>))).toEqual(record.decision);
    } else {
      expect(gate.cancelAll("forward") === 0).toBe(record.resolved);
    }
    expect(gate.pendingCount).toBe(record.pending_after);
  });

  it("stops counting a request once it has been decided", () => {
    // A count that never came down would make a busy tunnel look wedged.
    const gate = new InterceptGate(5);
    const waiting = gate.awaitDecision("r1", never);
    expect(gate.pendingCount).toBe(1);
    gate.resolve("r1", parseActionMessage({ action: "forward" }));
    expect(gate.pendingCount).toBe(0);
    return waiting;
  });

  it("keeps the first decision when a second arrives", () => {
    // Otherwise a duplicate message could overturn a drop somebody meant.
    const gate = new InterceptGate(5);
    const waiting = gate.awaitDecision("r1", never);
    gate.resolve("r1", parseActionMessage({ action: "drop" }));
    expect(gate.resolve("r1", parseActionMessage({ action: "forward" }))).toBe(false);
    return expect(waiting).resolves.toMatchObject({ action: "drop" });
  });

  it("releases a request nobody answered, the way it was told to", async () => {
    // Rather than leaving it waiting: a paused request nobody answers is a
    // request that never completes.
    for (const action of ["forward", "drop"] as const) {
      const gate = new InterceptGate(5, action);
      expect((await gate.awaitDecision("r1", async () => {})).action).toBe(action);
      expect(gate.pendingCount).toBe(0);
    }
  });

  it("ignores a decision that arrives after the time ran out", async () => {
    const gate = new InterceptGate(5, "forward");
    const decision = await gate.awaitDecision("r1", async () => {});
    expect(decision.action).toBe("forward");
    expect(gate.resolve("r1", parseActionMessage({ action: "drop" }))).toBe(false);
  });

  it("releases everything at once and empties the queue", async () => {
    const gate = new InterceptGate(5);
    const waits = ["a", "b", "c"].map((rid) => gate.awaitDecision(rid, never));
    expect(gate.cancelAll("drop")).toBe(3);
    expect(gate.pendingCount).toBe(0);
    for (const waiting of waits) {
      expect((await waiting).action).toBe("drop");
    }
  });

  it("waits for real when nobody says how", { timeout: 10_000 }, async () => {
    // The default wait is a real timer, and the floor is what stops a
    // misconfigured gate releasing everything the instant it pauses it.
    const gate = new InterceptGate(MIN_TIMEOUT_S, "drop");
    const started = Date.now();
    const decision = await gate.awaitDecision("r1");
    expect(decision.action).toBe("drop");
    expect(Date.now() - started).toBeGreaterThanOrEqual(MIN_TIMEOUT_S * 1000 - 50);
  });

  it("does nothing when the timer fires after a decision", { timeout: 10_000 }, async () => {
    // The ordinary case: somebody answers, and the timeout arrives afterwards
    // to find the request already gone.
    const gate = new InterceptGate(MIN_TIMEOUT_S, "drop");
    const waiting = gate.awaitDecision("r1");
    gate.resolve("r1", parseActionMessage({ action: "modify", headers: { "X-Trace": "1" } }));
    expect((await waiting).headers).toEqual({ "X-Trace": "1" });
    // Outlive the timer, which must not overturn what was decided.
    await new Promise((resolve) => setTimeout(resolve, MIN_TIMEOUT_S * 1000 + 100));
    expect(gate.pendingCount).toBe(0);
  });

  it("releases nothing when there is nothing waiting", () => {
    expect(new InterceptGate(5).cancelAll()).toBe(0);
  });

  it("forwards by default when told to release everything", async () => {
    // The same choice the parser makes for an action it cannot read: a tunnel
    // shutting down should let traffic through, not silently swallow it.
    const gate = new InterceptGate(5);
    const waiting = gate.awaitDecision("r1", never);
    expect(gate.cancelAll()).toBe(1);
    expect((await waiting).action).toBe("forward");
  });

  it("does not release the same request twice", async () => {
    const gate = new InterceptGate(5);
    const waiting = gate.awaitDecision("r1", never);
    expect(gate.cancelAll("drop")).toBe(1);
    expect(gate.cancelAll("drop")).toBe(0);
    expect((await waiting).action).toBe("drop");
  });

  it("keeps requests apart", async () => {
    const gate = new InterceptGate(5);
    const first = gate.awaitDecision("r1", never);
    const second = gate.awaitDecision("r2", never);
    gate.resolve("r1", parseActionMessage({ action: "drop" }));
    expect(gate.pendingCount).toBe(1);
    gate.resolve("r2", parseActionMessage({ action: "modify", headers: { "X-Trace": "1" } }));
    expect((await first).action).toBe("drop");
    expect((await second).headers).toEqual({ "X-Trace": "1" });
  });
});
