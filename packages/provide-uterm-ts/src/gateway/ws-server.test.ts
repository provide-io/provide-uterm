//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterAll, describe, expect, it } from "vitest";
import { WebSocket } from "ws";
import { SERVER_HOST } from "../defaults/index.ts";
import {
  DEFAULT_MAX_PAYLOAD_BYTES,
  pathOf,
  peerOf,
  type RunningWebSocketServer,
  resolveWebSocketServerOptions,
  servesPath,
  startWebSocketServer,
  type WebSocketHandler,
} from "./index.ts";

const running: RunningWebSocketServer[] = [];
afterAll(async () => {
  await Promise.all(running.map((server) => server.close()));
});

/** Port zero: the system picks, so tests never collide or hardcode one. */
const EPHEMERAL = 0;

/** Start a server on loopback, cleaned up at the end. */
async function serve(
  handler: WebSocketHandler,
  options: Parameters<typeof startWebSocketServer>[1] = {},
): Promise<RunningWebSocketServer> {
  const server = await startWebSocketServer(handler, { host: SERVER_HOST, port: EPHEMERAL, ...options });
  running.push(server);
  return server;
}

/** Connect, exchange, and return what came back. */
function converse(
  server: RunningWebSocketServer,
  send: Array<string | Uint8Array>,
  path = "/",
): Promise<Array<string | Uint8Array>> {
  return new Promise((resolve, reject) => {
    const client = new WebSocket(`ws://${server.host}:${server.port}${path}`);
    const received: Array<string | Uint8Array> = [];
    let failure: Error | undefined;
    client.on("open", () => {
      for (const message of send) {
        client.send(message);
      }
    });
    client.on("message", (data: Buffer, isBinary: boolean) => {
      received.push(isBinary ? Uint8Array.from(data) : data.toString("utf8"));
    });
    client.on("error", (error: Error) => {
      failure = error;
    });
    client.on("close", () => (failure === undefined ? resolve(received) : reject(failure)));
  });
}

describe("a live WebSocket server", () => {
  it("carries a message both ways", async () => {
    const server = await serve(async (connection) => {
      const message = await connection.receive();
      connection.send(`echo:${String(message)}`);
      connection.close();
    });
    expect(await converse(server, ["hello"])).toEqual(["echo:hello"]);
  });

  it("keeps text and binary apart", async () => {
    // The control channel reads them as different things: a frame decoded
    // from the wrong one would be read as the wrong kind entirely.
    const seen: string[] = [];
    const server = await serve(async (connection) => {
      for (let index = 0; index < 2; index += 1) {
        const message = await connection.receive();
        seen.push(typeof message === "string" ? "text" : "binary");
      }
      connection.close();
    });
    await converse(server, ["a text frame", Uint8Array.from([1, 2, 3])]);
    expect(seen).toEqual(["text", "binary"]);
  });

  it("sends binary back as binary", async () => {
    const server = await serve(async (connection) => {
      connection.send(Uint8Array.from([7, 8, 9]));
      connection.close();
    });
    const received = await converse(server, []);
    expect(received[0]).toBeInstanceOf(Uint8Array);
    expect([...(received[0] as Uint8Array)]).toEqual([7, 8, 9]);
  });

  it("reads a connection to its end", async () => {
    // A receive that never resolved would hang the handler forever.
    const server = await serve(async (connection) => {
      const first = await connection.receive();
      const second = await connection.receive();
      connection.send(`${String(first)}|${String(second)}`);
      connection.close();
    });
    const server2 = server;
    const received = await new Promise<string[]>((resolve) => {
      const client = new WebSocket(`ws://${server2.host}:${server2.port}/`);
      const out: string[] = [];
      client.on("open", () => {
        client.send("one");
        client.close();
      });
      client.on("message", (data: Buffer) => out.push(data.toString()));
      client.on("close", () => resolve(out));
    });
    expect(received).toEqual([]);
  });

  it("tells the handler where the client connected", async () => {
    let path: string | undefined;
    let peer: unknown;
    const server = await serve(async (connection) => {
      path = connection.path;
      peer = connection.peer;
      connection.close();
    });
    await converse(server, [], "/api/ws/term?worker=w1");
    expect(path).toBe("/api/ws/term");
    expect((peer as [string, number])[0]).toContain("127.0.0.1");
  });

  it("refuses a path it does not serve", async () => {
    // At the HTTP layer: a client that asked for a route this server does not
    // serve never becomes a WebSocket at all.
    const events: Array<Record<string, unknown>> = [];
    const server = await serve(async (connection) => connection.close(), {
      paths: ["/api/ws/term"],
      onEvent: (event, detail) => {
        if (event === "upgrade_refused") {
          events.push(detail);
        }
      },
    });
    await expect(converse(server, [], "/nope")).rejects.toThrow();
    expect(events[0]?.path).toBe("/nope");
    await expect(converse(server, [], "/api/ws/term")).resolves.toEqual([]);
  });

  it("matches the path without its query", async () => {
    // Otherwise a query string would smuggle a client onto a route it was
    // not offered — or keep it off one it was.
    const server = await serve(async (connection) => connection.close(), { paths: ["/api/ws/term"] });
    await expect(converse(server, [], "/api/ws/term?worker=w1")).resolves.toEqual([]);
  });

  it("keeps one connection's failure from taking the server down", async () => {
    let connections = 0;
    const events: string[] = [];
    const server = await serve(
      async (connection) => {
        connections += 1;
        if (connections === 1) {
          throw new Error("first connection fails");
        }
        connection.send("second works");
        connection.close();
      },
      { onEvent: (event) => events.push(event) },
    );
    await converse(server, []);
    expect(await converse(server, [])).toEqual(["second works"]);
    expect(events).toContain("connection_failed");
  });

  it("refuses a message larger than the cap", async () => {
    // The cap is what keeps one client from making the server allocate
    // whatever it likes. The connection is closed with 1009 rather than
    // erroring, which is what the protocol says "message too big" looks like.
    const server = await serve(
      async (connection) => {
        await connection.receive();
        connection.close();
      },
      { maxPayloadBytes: 1024 },
    );
    const code = await new Promise<number>((resolve) => {
      const client = new WebSocket(`ws://${server.host}:${server.port}/`);
      client.on("open", () => client.send("x".repeat(4096)));
      client.on("error", () => undefined);
      client.on("close", (closeCode: number) => resolve(closeCode));
    });
    expect(code).toBe(1009);
  });

  it("accepts a message at the cap", async () => {
    // The boundary is inclusive, so a client sending exactly the limit is not
    // disconnected for it.
    const server = await serve(
      async (connection) => {
        const message = await connection.receive();
        connection.send(String((message as string).length));
        connection.close();
      },
      { maxPayloadBytes: 1024 },
    );
    expect(await converse(server, ["x".repeat(1024)])).toEqual(["1024"]);
  });

  it("answers a plain HTTP request rather than hanging", async () => {
    // A health check or a stray browser hitting the port gets a refusal, not
    // a socket that never replies.
    const server = await serve(async (connection) => connection.close());
    const response = await fetch(`http://${server.host}:${server.port}/`);
    expect(response.status).toBe(426);
  });

  it("stops listening when closed", async () => {
    const server = await startWebSocketServer(async (connection) => connection.close(), {
      host: SERVER_HOST,
      port: EPHEMERAL,
    });
    await server.close();
    await expect(converse(server, [])).rejects.toThrow();
  });

  it("reports the port it started on", async () => {
    const events: Array<{ event: string; detail: Record<string, unknown> }> = [];
    const server = await serve(async (connection) => connection.close(), {
      onEvent: (event, detail) => events.push({ event, detail }),
    });
    expect(events[0]?.event).toBe("server_started");
    expect(events[0]?.detail.port).toBe(server.port);
  });
});

describe("the defaults a server fills in", () => {
  it("binds every address by default", () => {
    expect(resolveWebSocketServerOptions({}).host).toBe("0.0.0.0");
  });

  it("takes an ephemeral port by default", () => {
    // Unlike SSH, there is no conventional port for this; the caller is
    // expected to say.
    expect(resolveWebSocketServerOptions({}).port).toBe(0);
  });

  it("caps a payload at the same size the Worker does", () => {
    expect(resolveWebSocketServerOptions({}).maxPayloadBytes).toBe(DEFAULT_MAX_PAYLOAD_BYTES);
    expect(DEFAULT_MAX_PAYLOAD_BYTES).toBe(1_048_576);
  });

  it("keeps what it was given", () => {
    expect(resolveWebSocketServerOptions({ host: "10.0.0.1", port: 8080, maxPayloadBytes: 1 })).toEqual({
      host: "10.0.0.1",
      port: 8080,
      maxPayloadBytes: 1,
    });
  });
});

describe("reading a request", () => {
  it("takes the path without its query or fragment", () => {
    expect(pathOf("/a/b?x=1")).toBe("/a/b");
    expect(pathOf("/a/b#top")).toBe("/a/b");
    expect(pathOf("/a/b?x=1#top")).toBe("/a/b");
    expect(pathOf("/a/b")).toBe("/a/b");
  });

  it("treats a missing target as the empty path", () => {
    expect(pathOf(undefined)).toBe("");
    expect(pathOf("")).toBe("");
  });

  it("reads the address a socket reports", () => {
    expect(peerOf({ remoteAddress: "10.0.0.2", remotePort: 5003 })).toEqual(["10.0.0.2", 5003]);
  });

  it("supplies a port for an address that arrives without one", () => {
    expect(peerOf({ remoteAddress: "10.0.0.2" })).toEqual(["10.0.0.2", 0]);
  });

  it("reports nothing for a socket with no address", () => {
    // Which a socket that has already gone away reports.
    expect(peerOf({})).toBeUndefined();
    expect(peerOf({ remotePort: 5003 })).toBeUndefined();
  });
});

describe("which paths a server serves", () => {
  it("serves everything when it names nothing", () => {
    expect(servesPath(undefined, "/anything")).toBe(true);
    expect(servesPath([], "/anything")).toBe(true);
  });

  it("serves a closed set when it names one", () => {
    expect(servesPath(["/a", "/b"], "/a")).toBe(true);
    expect(servesPath(["/a", "/b"], "/c")).toBe(false);
  });

  it("ignores the query and the fragment", () => {
    expect(servesPath(["/a"], "/a?x=1")).toBe(true);
    expect(servesPath(["/a"], "/a#top")).toBe(true);
    expect(servesPath(["/a"], "/ab")).toBe(false);
  });

  it("treats a missing url as the empty path", () => {
    expect(servesPath(["/a"], undefined)).toBe(false);
    expect(servesPath([""], undefined)).toBe(true);
  });
});
