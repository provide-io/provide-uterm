//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, describe, expect, it, vi } from "vitest";
import { authHeaders, BAD_TOKEN, errorMessage, FetchTransport } from "./index.ts";

/** What a fetch was asked for. */
interface Recorded {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

/** A fetch that answers with one response and writes down what it was asked. */
function recorder(status = 200, body = '{"ok":true}') {
  const seen: Recorded[] = [];
  const fetchImpl = (async (input: unknown, init?: RequestInit) => {
    seen.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers: { ...(init?.headers as Record<string, string>) },
      body: typeof init?.body === "string" ? init.body : null,
    });
    return new Response(body, { status });
  }) as unknown as typeof fetch;
  return { seen, fetchImpl };
}

/** A transport pointed at a fake server. */
function transportFor(seen: ReturnType<typeof recorder>, auth: "token" | "none" | "bad" = "token") {
  return new FetchTransport({ baseUrl: "http://127.0.0.1:9/", token: "issued", auth, fetchImpl: seen.fetchImpl });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the header a step presents", () => {
  it("sends the server's token by default", () => {
    expect(authHeaders("token", "issued")).toStrictEqual({ Authorization: "Bearer issued" });
  });

  it("sends no Authorization header at all for `none`", () => {
    // Not an empty one: an empty header is a header, and a server may treat
    // the two differently — which is exactly what the scenario is asking.
    expect(authHeaders("none", "issued")).toStrictEqual({});
  });

  it("sends a token no server issued for `bad`", () => {
    expect(authHeaders("bad", "issued")).toStrictEqual({ Authorization: `Bearer ${BAD_TOKEN}` });
    expect(BAD_TOKEN).not.toBe("issued");
  });
});

describe("the message for anything thrown", () => {
  it("takes an Error's message", () => {
    expect(errorMessage(new Error("connect ECONNREFUSED"))).toBe("connect ECONNREFUSED");
  });

  it("spells out the cause underneath it", () => {
    // Node's fetch rejects with `fetch failed` and puts the reason in the
    // cause. Without it, a refused connection and a name that did not
    // resolve are the same line in the matrix.
    const failed = new Error("fetch failed", { cause: new Error("connect ECONNREFUSED 127.0.0.1:1") });

    expect(errorMessage(failed)).toBe("fetch failed: connect ECONNREFUSED 127.0.0.1:1");
  });

  it("takes anything else as it prints", () => {
    // A rejected fetch is usually an Error, but a driver that crashed on a
    // thrown string would report nothing at all.
    expect(errorMessage("boom")).toBe("boom");
  });
});

describe("the driver's transport", () => {
  it("puts the path onto the base URL and asks for JSON", async () => {
    const seen = recorder();
    const transport = transportFor(seen);

    const response = await transport.request("GET", "/api/health");

    // The trailing slash on the base URL is dropped rather than doubled: a
    // `//api/health` is a different path to some servers.
    expect(seen.seen[0]?.url).toBe("http://127.0.0.1:9/api/health");
    expect(seen.seen[0]?.method).toBe("GET");
    expect(seen.seen[0]?.headers).toStrictEqual({ Accept: "application/json", Authorization: "Bearer issued" });
    expect(seen.seen[0]?.body).toBeNull();
    expect(response.status).toBe(200);
    expect(response.json()).toStrictEqual({ ok: true });
    expect(transport.attempt).toStrictEqual({ status: 200, jsonOk: true, error: null });
  });

  it("carries query parameters", async () => {
    const seen = recorder();

    await transportFor(seen).request("GET", "/api/x", { params: { wait_ms: 10 } });

    expect(seen.seen[0]?.url).toBe("http://127.0.0.1:9/api/x?wait_ms=10");
  });

  it("sends a JSON body when there is one", async () => {
    const seen = recorder();

    await transportFor(seen).request("POST", "/api/x", { json: { a: 1 } });

    expect(seen.seen[0]?.method).toBe("POST");
    expect(seen.seen[0]?.body).toBe('{"a":1}');
    expect(seen.seen[0]?.headers["Content-Type"]).toBe("application/json");
  });

  it("sends no body, and no content type, when there is none", async () => {
    const seen = recorder();

    await transportFor(seen).request("POST", "/api/x");

    expect(seen.seen[0]?.body).toBeNull();
    expect(seen.seen[0]?.headers["Content-Type"]).toBeUndefined();
  });

  it("omits the Authorization header for an unauthenticated step", async () => {
    const seen = recorder();

    await transportFor(seen, "none").request("GET", "/api/health");

    expect(seen.seen[0]?.headers.Authorization).toBeUndefined();
  });

  it("presents a token no server issued for a bad-auth step", async () => {
    const seen = recorder();

    await transportFor(seen, "bad").request("GET", "/api/health");

    expect(seen.seen[0]?.headers.Authorization).toBe(`Bearer ${BAD_TOKEN}`);
  });

  it("records the status of a refusal, and hands the body over unchanged", async () => {
    const seen = recorder(401, '{"detail":"no"}');
    const transport = transportFor(seen);

    const response = await transport.request("GET", "/api/sessions");

    expect(response.status).toBe(401);
    expect(transport.attempt.status).toBe(401);
    expect(response.text).toBe('{"detail":"no"}');
  });

  it("marks a body that is not JSON, and still throws for the client", async () => {
    // The client library reads a body by parsing it; the driver has to know
    // the parse failed, because the protocol records that as `<non-json>`
    // rather than as the bytes.
    const seen = recorder(200, "<html>hello</html>");
    const transport = transportFor(seen);

    const response = await transport.request("GET", "/");

    expect(() => response.json()).toThrow();
    expect(transport.attempt.jsonOk).toBe(false);
    expect(response.text).toBe("<html>hello</html>");
  });

  it("writes down a transport that failed, and lets it through", async () => {
    const fetchImpl = (async () => {
      throw new Error("connect ECONNREFUSED 127.0.0.1:9");
    }) as unknown as typeof fetch;
    const transport = new FetchTransport({ baseUrl: "http://127.0.0.1:9", token: "t", auth: "token", fetchImpl });

    await expect(transport.request("GET", "/api/health")).rejects.toThrow("connect ECONNREFUSED");
    expect(transport.attempt).toStrictEqual({
      status: null,
      jsonOk: true,
      error: "connect ECONNREFUSED 127.0.0.1:9",
    });
  });

  it("starts each request from a clean slate", async () => {
    const first = recorder(500, "not json");
    const transport = transportFor(first);
    await transport.request("GET", "/a").then((response) => {
      expect(() => response.json()).toThrow();
    });
    expect(transport.attempt).toStrictEqual({ status: 500, jsonOk: false, error: null });

    // The same transport used again must not report the first request's
    // observations, or a second step would inherit a first step's failure.
    const answer = await transport.request("GET", "/b");
    expect(answer.status).toBe(500);
    expect(transport.attempt.jsonOk).toBe(true);
  });

  it("uses the runtime's own fetch when it is given none", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: unknown) => {
      calls.push(String(input));
      return new Response("{}", { status: 200 });
    });
    const transport = new FetchTransport({ baseUrl: "http://127.0.0.1:9", token: "t", auth: "token" });

    const response = await transport.request("GET", "/api/health");

    expect(calls).toStrictEqual(["http://127.0.0.1:9/api/health"]);
    expect(response.json()).toStrictEqual({});
  });
});
