//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The Node binding, exercised over a real socket.
 *
 * There is no useful way to fake this one: what it is for is the translation
 * between Node's streams and the `Request`/`Response` pair the application
 * speaks, and a mock of Node's `http` would be a test of the mock. So every
 * case below binds an ephemeral port and talks to it.
 */

import type { IncomingMessage } from "node:http";
import { describe, expect, it } from "vitest";
import type { ServerApp } from "./app.ts";
import { bootstrapServer } from "./bootstrap.ts";
import { type RunningServer, serveApp, toRequest } from "./node-http.ts";

/** Run something against a server, and always take the server down after. */
async function withServer(app: ServerApp, body: (server: RunningServer) => Promise<void>): Promise<void> {
  const server = await serveApp(app);
  try {
    await body(server);
  } finally {
    await server.close();
  }
}

/** An application that answers with whatever it was asked. */
function echo(): ServerApp {
  return {
    ready: true,
    handle: async (request) => {
      return Response.json({
        method: request.method,
        path: new URL(request.url).pathname,
        authorization: request.headers.get("authorization"),
        cookies: request.headers.getSetCookie === undefined ? [] : [],
        body: request.method === "GET" ? null : await request.text(),
      });
    },
  };
}

describe("serving over a socket", () => {
  it("binds a port the operating system chose and says which", async () => {
    // Nothing in this repository may name a port; the driver protocol is
    // built on the server reporting what it was given.
    await withServer(echo(), async (server) => {
      expect(server.port).toBeGreaterThan(0);
      expect(server.baseUrl).toBe(`http://127.0.0.1:${server.port}`);
      expect(server.host).toBe("127.0.0.1");
    });
  });

  it("carries a method, a path and a header through", async () => {
    await withServer(echo(), async (server) => {
      const response = await fetch(`${server.baseUrl}/some/where`, { headers: { Authorization: "Bearer x" } });
      expect(await response.json()).toMatchObject({
        method: "GET",
        path: "/some/where",
        authorization: "Bearer x",
        body: null,
      });
    });
  });

  it("carries a body for the methods that may have one", async () => {
    await withServer(echo(), async (server) => {
      const response = await fetch(`${server.baseUrl}/`, { method: "POST", body: '{"a":1}' });
      expect(((await response.json()) as { body: string }).body).toBe('{"a":1}');
    });
  });

  it("carries a header sent more than once as more than one value", async () => {
    // Node hands a repeated header over as a list, and joining them would
    // change what a receiver reads — a `Set-Cookie` pair being the case where
    // that matters. Driven through `toRequest` directly, because the client
    // used above folds them before they ever reach the socket.
    const message = {
      method: "GET",
      url: "/",
      headers: { "set-cookie": ["a=1", "b=2"], "x-one": "1", "x-absent": undefined },
    } as unknown as IncomingMessage;
    const request = await toRequest(message, "http://127.0.0.1:1");
    expect(request.headers.getSetCookie()).toEqual(["a=1", "b=2"]);
    expect(request.headers.get("x-one")).toBe("1");
    // A header Node reports as absent is absent rather than the word.
    expect(request.headers.get("x-absent")).toBeNull();
  });

  it("has a path even when Node reports none", async () => {
    const message = { method: "GET", headers: {} } as unknown as IncomingMessage;
    expect(new URL((await toRequest(message, "http://127.0.0.1:1")).url).pathname).toBe("/");
  });

  it("has a method even when Node reports none", async () => {
    const message = { url: "/", headers: {} } as unknown as IncomingMessage;
    expect((await toRequest(message, "http://127.0.0.1:1")).method).toBe("GET");
  });

  it("carries the status and the headers back out", async () => {
    const app: ServerApp = {
      ready: true,
      handle: async () => new Response("no", { status: 418, headers: { "x-teapot": "yes" } }),
    };
    await withServer(app, async (server) => {
      const response = await fetch(`${server.baseUrl}/`);
      expect(response.status).toBe(418);
      expect(response.headers.get("x-teapot")).toBe("yes");
      expect(await response.text()).toBe("no");
    });
  });

  it("answers a handler that threw rather than leaving the client hanging", async () => {
    // A client left waiting cannot tell a crash from a slow response, and
    // will wait as long as its own timeout says.
    const app: ServerApp = {
      ready: true,
      handle: async () => {
        throw new Error("nope");
      },
    };
    await withServer(app, async (server) => {
      const response = await fetch(`${server.baseUrl}/`);
      expect(response.status).toBe(500);
      expect(await response.json()).toEqual({ detail: "Internal Server Error" });
    });
  });

  it("refuses to resolve when the address cannot be bound", async () => {
    // Rejecting rather than waiting for a `listening` that will never come:
    // a driver that hung here would cost the harness its whole grace period.
    await withServer(echo(), async (server) => {
      await expect(serveApp(echo(), { port: server.port })).rejects.toThrow();
    });
  });

  it("serves the real application it was given", async () => {
    const { app, token } = bootstrapServer({ authMode: "dev_token" });
    await withServer(app, async (server) => {
      const health = await fetch(`${server.baseUrl}/api/health`);
      expect(health.status).toBe(200);
      const sessions = await fetch(`${server.baseUrl}/api/sessions`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(((await sessions.json()) as { session_id: string }[])[0]?.session_id).toBe("provide-shell");
    });
  });

  it("stops when it is closed, and closing waits until it has", async () => {
    const server = await serveApp(echo());
    const url = server.baseUrl;
    await server.close();
    await expect(fetch(url)).rejects.toThrow();
  });
});
