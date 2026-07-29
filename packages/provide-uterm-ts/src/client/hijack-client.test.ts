//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  DEFAULT_ENTITY_PREFIX,
  DEFAULT_EVENT_LIMIT,
  DEFAULT_LEASE_S,
  DEFAULT_POLL_INTERVAL_MS,
  DEFAULT_SEND_TIMEOUT_MS,
  DEFAULT_SNAPSHOT_WAIT_MS,
  HijackClient,
  type HijackTransport,
} from "./index.ts";

interface RequestsGolden {
  calls: Array<{
    name: string;
    calls: Array<{ method: string; path: string; json: unknown; params: unknown }>;
    ok: boolean;
    answer: unknown;
    status: number;
    no_json: boolean;
  }>;
}

const golden = loadGolden<RequestsGolden>("hijackrequests_golden.json");

/** A transport that writes down what it was asked for. */
function recorder(status = 200, body: unknown = { ok: true }) {
  const calls: Array<{ method: string; path: string; json: unknown; params: unknown }> = [];
  const transport: HijackTransport = {
    request: async (method, path, options) => {
      calls.push({ method, path, json: options.json ?? null, params: options.params ?? null });
      return {
        status,
        json: () => {
          if (body === null) {
            throw new Error("no json");
          }
          return body;
        },
        text: body === null ? "" : JSON.stringify(body),
      };
    },
  };
  return { calls, transport };
}

/** Every call in the corpus, by name, applied to a client. */
const INVOKE: Record<string, (client: HijackClient) => Promise<unknown>> = {
  "acquiring a hijack": (c) => c.acquire("w1"),
  "acquiring with an owner and a lease": (c) => c.acquire("w1", { owner: "ada", leaseS: 30 }),
  "a heartbeat": (c) => c.heartbeat("w1", "h1"),
  "a heartbeat with a lease": (c) => c.heartbeat("w1", "h1", { leaseS: 15 }),
  "sending keys": (c) => c.send("w1", "h1", { keys: "ls\n" }),
  "sending keys with an expected prompt": (c) => c.send("w1", "h1", { keys: "ls\n", expectPromptId: "p1" }),
  "sending keys with an expected pattern": (c) => c.send("w1", "h1", { keys: "ls\n", expectRegex: "^\\$ " }),
  "sending keys with both expectations and timings": (c) =>
    c.send("w1", "h1", {
      keys: "ls\n",
      expectPromptId: "p1",
      expectRegex: "x",
      timeoutMs: 50,
      pollIntervalMs: 5,
    }),
  stepping: (c) => c.step("w1", "h1"),
  releasing: (c) => c.release("w1", "h1"),
  "a snapshot": (c) => c.snapshot("w1", "h1"),
  "a snapshot with a wait": (c) => c.snapshot("w1", "h1", { waitMs: 10 }),
  events: (c) => c.events("w1", "h1"),
  "events after a sequence": (c) => c.events("w1", "h1", { afterSeq: 7, limit: 5 }),
  "a screenshot": (c) => c.guiScreenshot("w1", "h1"),
  "a click": (c) => c.guiClick("w1", "h1", 10, 20),
  "a right click": (c) => c.guiClick("w1", "h1", 10, 20, "right"),
  typing: (c) => c.guiType("w1", "h1", "hello"),
  "a key": (c) => c.guiKey("w1", "h1", "Return"),
  "a drag": (c) => c.guiDrag("w1", "h1", 1, 2, 3, 4),
  "setting the input mode": (c) => c.setInputMode("w1", "hijack"),
  "disconnecting a worker": (c) => c.disconnectWorker("w1"),
  health: (c) => c.health(),
  "listing sessions": (c) => c.listSessions(),
  "one session": (c) => c.getSession("sess-1"),
  "a session snapshot": (c) => c.sessionSnapshot("sess-1"),
  "a refusal": (c) => c.step("w1", "h1"),
  "a server fault": (c) => c.step("w1", "h1"),
  "an answer that is not json": (c) => c.step("w1", "h1"),
  "a created answer": (c) => c.acquire("w1"),
  "an answer with no content": (c) => c.release("w1", "h1"),
};

describe("what the client asks for", () => {
  it.each(golden.calls)("$name", async (record) => {
    // `null` is a body somebody might mean, so "there is none" is said with a
    // flag rather than smuggled through the value.
    const { calls, transport } = recorder(record.status, record.no_json ? null : (record.answer as unknown));
    const client = new HijackClient({ transport });
    const answer = (await (INVOKE[record.name] as (c: HijackClient) => Promise<unknown>)(client)) as {
      ok: boolean;
      body: unknown;
    };

    expect(calls).toEqual(record.calls);
    expect(answer.ok).toBe(record.ok);
    expect(answer.body).toEqual(record.answer);
  });

  it("leaves out an expectation nobody set", async () => {
    // Absent rather than null: a server reading a null may take it for an
    // instruction.
    const { calls, transport } = recorder();
    await new HijackClient({ transport }).send("w1", "h1", { keys: "ls" });
    expect(Object.keys(calls[0]?.json as object).sort()).toEqual(["keys", "poll_interval_ms", "timeout_ms"]);
  });

  it("leaves out an expectation set to nothing", async () => {
    // This runtime has two ways of saying nothing where the reference has
    // one, and a server reading a null may take it for an instruction.
    const { calls, transport } = recorder();
    await new HijackClient({ transport }).send("w1", "h1", {
      keys: "ls",
      expectPromptId: undefined,
      expectRegex: null as unknown as string,
    });
    expect(Object.keys(calls[0]?.json as object).sort()).toEqual(["keys", "poll_interval_ms", "timeout_ms"]);
  });

  it("sends an expectation somebody did set", async () => {
    const { calls, transport } = recorder();
    await new HijackClient({ transport }).send("w1", "h1", { keys: "ls", expectRegex: "^\\$ " });
    expect((calls[0]?.json as Record<string, unknown>).expect_regex).toBe("^\\$ ");
  });

  it("uses the defaults the reference uses", () => {
    expect(DEFAULT_ENTITY_PREFIX).toBe("/worker");
    expect(DEFAULT_LEASE_S).toBe(90);
    expect(DEFAULT_SEND_TIMEOUT_MS).toBe(2000);
    expect(DEFAULT_POLL_INTERVAL_MS).toBe(120);
    expect(DEFAULT_SNAPSHOT_WAIT_MS).toBe(1500);
    expect(DEFAULT_EVENT_LIMIT).toBe(200);
  });

  it("puts a worker's routes where it was told to", async () => {
    const { calls, transport } = recorder();
    await new HijackClient({ transport, entityPrefix: "/api/workers" }).step("w1", "h1");
    expect(calls[0]?.path).toBe("/api/workers/w1/hijack/h1/step");
  });

  it("refuses an identifier that would forge a route, before asking anything", async () => {
    const { calls, transport } = recorder();
    const client = new HijackClient({ transport });
    await expect(client.step("../admin", "h1")).rejects.toThrow("invalid worker_id");
    await expect(client.step("w1", "../h")).rejects.toThrow("invalid hijack_id");
    await expect(client.getSession("../workers")).rejects.toThrow("invalid session_id");
    expect(calls).toEqual([]);
  });
});

describe("what the client makes of an answer", () => {
  it("takes any success as success", async () => {
    for (const status of [200, 201, 204, 299]) {
      const { transport } = recorder(status, { fine: true });
      expect((await new HijackClient({ transport }).step("w1", "h1")).ok).toBe(true);
    }
  });

  it("takes only a 2xx as success", async () => {
    // Not a redirect, and not an informational: the reference asks whether
    // the status is a success, and a 3xx is a server saying "look elsewhere".
    for (const status of [100, 199, 300, 302, 304, 399]) {
      const { transport } = recorder(status, { moved: true });
      expect((await new HijackClient({ transport }).step("w1", "h1")).ok).toBe(false);
    }
  });

  it("takes anything else as a failure, and keeps what was said", async () => {
    // A caller driving a terminal needs to show somebody what happened.
    for (const status of [400, 401, 409, 500, 503]) {
      const { transport } = recorder(status, { error: "no" });
      const answer = await new HijackClient({ transport }).step("w1", "h1");
      expect(answer.ok).toBe(false);
      expect(answer.body).toEqual({ error: "no" });
    }
  });

  it("hands back an answer that is not JSON as it arrived", async () => {
    const { transport } = recorder(200, null);
    expect((await new HijackClient({ transport }).step("w1", "h1")).body).toEqual({ raw: "" });
  });

  it("does not raise when the transport itself fails", async () => {
    // A network that is down is an answer, not an exception to unwind on.
    const transport: HijackTransport = {
      request: async () => {
        throw new Error("connection refused");
      },
    };
    const answer = await new HijackClient({ transport }).step("w1", "h1");
    expect(answer.ok).toBe(false);
    expect(answer.body).toEqual({ error: "connection refused" });
  });

  it("says something useful when the transport threw something that is not an error", async () => {
    const transport: HijackTransport = {
      request: async () => {
        throw "just a string";
      },
    };
    expect((await new HijackClient({ transport }).step("w1", "h1")).body).toEqual({ error: "just a string" });
  });
});

describe("what the client writes down", () => {
  it("reports a failed call with its method, path and status", async () => {
    const seen: Array<[string, string, number | undefined, unknown]> = [];
    const { transport } = recorder(409, { error: "held" });
    await new HijackClient({
      transport,
      logger: { requestFailed: (method, path, status, body) => seen.push([method, path, status, body]) },
    }).step("w1", "h1");
    expect(seen).toEqual([["POST", "/worker/w1/hijack/h1/step", 409, { error: "held" }]]);
  });

  it("strips a secret out of a failure before writing it", async () => {
    // A failure body can hold a token, and a log is where it would stay.
    const seen: unknown[] = [];
    const { transport } = recorder(401, { error: "denied", token: "s3cret" });
    await new HijackClient({
      transport,
      logger: { requestFailed: (_method, _path, _status, body) => seen.push(body) },
    }).step("w1", "h1");
    expect(seen).toEqual([{ error: "denied", token: "***" }]);
  });

  it("hands the caller the real body, not the stripped one", async () => {
    // The stripping is for the log; a caller may need to act on what was
    // actually said.
    const { transport } = recorder(401, { token: "s3cret" });
    const answer = await new HijackClient({ transport }).step("w1", "h1");
    expect(answer.body).toEqual({ token: "s3cret" });
  });

  it("says nothing about a call that worked", async () => {
    let called = 0;
    const { transport } = recorder(200, { fine: true });
    await new HijackClient({
      transport,
      logger: {
        requestFailed: () => {
          called += 1;
        },
      },
    }).step("w1", "h1");
    expect(called).toBe(0);
  });

  it("reports a transport failure with no status at all", async () => {
    // There was no response, so naming one would be inventing it.
    const seen: Array<number | undefined> = [];
    const transport: HijackTransport = {
      request: async () => {
        throw new Error("down");
      },
    };
    await new HijackClient({
      transport,
      logger: { requestFailed: (_method, _path, status) => seen.push(status) },
    }).step("w1", "h1");
    expect(seen).toEqual([undefined]);
  });
});
