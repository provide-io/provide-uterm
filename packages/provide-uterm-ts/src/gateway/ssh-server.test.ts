//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { mkdtempSync, rmSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "ssh2";
import { afterAll, describe, expect, it } from "vitest";
import { SERVER_HOST, SSH_PORT } from "../defaults/index.ts";
import {
  DEFAULT_MAX_CONNECTIONS_PER_IP,
  generateHostKey,
  PermissiveAuthError,
  peerFrom,
  type RunningSshServer,
  resolveSshServerOptions,
  type SshConnectionHandler,
  startSshServer,
} from "./index.ts";

const scratch = mkdtempSync(join(tmpdir(), "uterm-sshd-"));
const running: RunningSshServer[] = [];

afterAll(async () => {
  await Promise.all(running.map((server) => server.close()));
  rmSync(scratch, { recursive: true, force: true });
});

/** Port zero: the system picks, so tests never collide or hardcode one. */
const EPHEMERAL = 0;

/** Start a server bound to loopback, cleaned up at the end. */
async function serve(
  handler: SshConnectionHandler,
  options: Partial<Parameters<typeof startSshServer>[1]> = {},
): Promise<RunningSshServer> {
  const server = await startSshServer(handler, {
    host: SERVER_HOST,
    port: EPHEMERAL,
    hostKeyDir: join(scratch, "keys"),
    ...options,
  });
  running.push(server);
  return server;
}

/**
 * Connect, run a shell session, and return what the server sent back.
 *
 * Waits for the client's own close, not merely the stream's: a slot is given
 * back when the connection ends, so a test that reconnects immediately would
 * otherwise race the release.
 */
function converse(
  server: RunningSshServer,
  send: string,
  credentials: Record<string, unknown> = { username: "u", password: "p" },
): Promise<string> {
  return new Promise((resolve, reject) => {
    const client = new Client();
    let received = "";
    let ready = false;
    let failure: Error | undefined;

    client
      .on("ready", () => {
        ready = true;
        client.shell((error, stream) => {
          if (error) {
            failure = error;
            client.end();
            return;
          }
          stream.on("data", (chunk: Buffer) => {
            received += chunk.toString();
          });
          stream.on("close", () => client.end());
          stream.write(send);
          // The server may wait for the end of input; without this a handler
          // that reads to EOF never returns.
          stream.end();
        });
      })
      .on("error", (error: Error) => {
        failure = error;
      })
      .on("close", () => {
        if (failure !== undefined) {
          reject(failure);
        } else if (!ready) {
          reject(new Error("connection closed before it was ready"));
        } else {
          resolve(received);
        }
      })
      .connect({
        host: server.host,
        port: server.port,
        // The host key is generated per test run, so there is nothing to pin.
        hostVerifier: () => true,
        ...credentials,
      });
  });
}

describe("a live SSH server", () => {
  it("carries bytes both ways", async () => {
    // The reference marks its process factory no-cover as a live-connection
    // callback. This is that callback, actually connected to.
    const server = await serve(async (reader, writer) => {
      const data = await reader.read();
      writer.write(Buffer.from(`echo:${Buffer.from(data).toString()}`));
      await writer.drain();
      writer.close();
    });
    expect(await converse(server, "hello")).toBe("echo:hello");
  });

  it("binds the port it was given back", async () => {
    // Asking for zero and being told which port was taken is what makes a
    // test able to connect at all.
    const server = await serve(async (_reader, writer) => writer.close());
    expect(server.port).toBeGreaterThan(0);
    expect(server.host).toBe(SERVER_HOST);
  });

  it("tells the session who connected", async () => {
    let peer: unknown;
    const server = await serve(async (_reader, writer) => {
      peer = writer.getExtraInfo("peername");
      writer.close();
    });
    await converse(server, "x");
    expect(Array.isArray(peer)).toBe(true);
    expect((peer as [string, number])[0]).toBe(SERVER_HOST);
  });

  it("knows only who connected, not how", async () => {
    // The reference exposes exactly one field; anything else takes the
    // caller's default rather than leaking whatever the runtime happened to
    // carry.
    let other: unknown = "unset";
    const server = await serve(async (_reader, writer) => {
      other = writer.getExtraInfo("cipher", "fallback");
      writer.close();
    });
    await converse(server, "x");
    expect(other).toBe("fallback");
  });

  it("accepts a key the validator accepts", async () => {
    // The other authentication method, which reaches a different arm of the
    // check than a password does.
    const clientKey = generateHostKey("client");
    const offered: string[] = [];
    const server = await serve(
      async (_reader, writer) => {
        writer.write(Buffer.from("keyed in"));
        writer.close();
      },
      {
        publicKeyValidator: (user, key) => {
          offered.push(user);
          return (key as { algo?: string })?.algo === "ssh-ed25519";
        },
      },
    );
    expect(await converse(server, "x", { username: "u", privateKey: clientKey })).toBe("keyed in");
    expect(offered).toContain("u");
  });

  it("refuses a key the validator rejects", async () => {
    const server = await serve(async (_reader, writer) => writer.close(), {
      publicKeyValidator: () => false,
    });
    await expect(converse(server, "x", { username: "u", privateKey: generateHostKey("client") })).rejects.toThrow();
  });

  it("reads a session to its end", async () => {
    // A reader that never saw the close would hang the session forever.
    const server = await serve(async (reader, writer) => {
      const first = await reader.read();
      const second = await reader.read();
      writer.write(Buffer.from(`${Buffer.from(first).toString()}|${second.length}`));
      writer.close();
    });
    expect(await converse(server, "one")).toBe("one|0");
  });

  it("refuses a password the validator rejects", async () => {
    const server = await serve(async (_reader, writer) => writer.close(), {
      credentialsValidator: (user, password) => user === "u" && password === "right", // pragma: allowlist secret
    });
    await expect(converse(server, "x", { username: "u", password: "wrong" })).rejects.toThrow(); // pragma: allowlist secret
    expect(await converse(server, "x", { username: "u", password: "right" })).toBe(""); // pragma: allowlist secret
  });

  it("keeps one session's failure from taking the server down", async () => {
    // A handler that throws ends its own session and nothing else.
    let sessions = 0;
    const events: string[] = [];
    const server = await serve(
      async (_reader, writer) => {
        sessions += 1;
        if (sessions === 1) {
          throw new Error("first session fails");
        }
        writer.write(Buffer.from("second session works"));
        writer.close();
      },
      { onEvent: (event) => events.push(event) },
    );
    await converse(server, "x");
    expect(await converse(server, "x")).toBe("second session works");
    expect(events).toContain("session_failed");
  });

  it("caps concurrent connections from one address", async () => {
    // The limiter was ported and tested on its own; this is it actually
    // refusing a connection.
    const events: Array<Record<string, unknown>> = [];
    const server = await serve(async (_reader, writer) => writer.close(), {
      maxConnectionsPerIp: 1,
      onEvent: (event, detail) => {
        if (event === "connection_rejected") {
          events.push(detail);
        }
      },
    });

    // Hold one connection open while a second is attempted.
    const held = new Client();
    await new Promise<void>((resolve, reject) => {
      held
        .on("ready", () => resolve())
        .on("error", reject)
        .connect({
          host: server.host,
          port: server.port,
          username: "u",
          password: "p",
          hostVerifier: () => true,
        });
    });
    try {
      await expect(converse(server, "x")).rejects.toThrow();
      expect(events).toHaveLength(1);
      expect(events[0]?.reason).toBe("per-ip limit");
    } finally {
      held.end();
    }
  });

  it("gives a slot back when a connection ends", async () => {
    // Otherwise one connection would exhaust a cap of one for the life of
    // the process.
    const server = await serve(async (_reader, writer) => writer.close(), { maxConnectionsPerIp: 1 });
    await converse(server, "x");
    await converse(server, "x");
    expect(await converse(server, "x")).toBe("");
  });

  it("survives a client that is not speaking SSH", async () => {
    // A port scanner, a stray HTTP request, a broken client. None of them may
    // take the server down, and each is reported rather than swallowed.
    const events: string[] = [];
    const server = await serve(async (_reader, writer) => writer.close(), {
      onEvent: (event) => events.push(event),
    });

    await new Promise<void>((resolve) => {
      const socket = connect(server.port, server.host, () => {
        // A plausible banner followed by nonsense, so the failure happens in
        // the key exchange rather than the server waiting for more banner.
        socket.write("SSH-2.0-bogus\r\n");
        socket.write(Buffer.alloc(64, 0xff));
        socket.destroy();
      });
      socket.on("close", () => resolve());
      socket.on("error", () => resolve());
    });

    // Wait for the server to notice, without pinning how long it takes.
    for (let attempt = 0; attempt < 50 && !events.includes("client_error"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    expect(events).toContain("client_error");
    // The server is still answering.
    expect(await converse(server, "x")).toBe("");
  });

  it("reports the port it started on", async () => {
    const events: Array<{ event: string; detail: Record<string, unknown> }> = [];
    const server = await serve(async (_reader, writer) => writer.close(), {
      onEvent: (event, detail) => events.push({ event, detail }),
    });
    expect(events[0]?.event).toBe("server_started");
    expect(events[0]?.detail.port).toBe(server.port);
  });

  it("stops listening when closed", async () => {
    const server = await startSshServer(async (_reader, writer) => writer.close(), {
      host: SERVER_HOST,
      port: EPHEMERAL,
      hostKeyDir: join(scratch, "keys"),
    });
    await server.close();
    await expect(converse(server, "x")).rejects.toThrow();
  });
});

describe("the defaults a server fills in", () => {
  // Asserted without binding: taking the default port would mean holding
  // 2222, which is neither reliable nor this test's business.

  it("binds every address by default", () => {
    // Which is exactly why the permissive-auth refusal exists.
    expect(resolveSshServerOptions({ hostKeyDir: "x" }).host).toBe("0.0.0.0");
  });

  it("takes the platform's SSH port by default", () => {
    expect(resolveSshServerOptions({ hostKeyDir: "x" }).port).toBe(SSH_PORT);
  });

  it("caps connections per address by default", () => {
    expect(resolveSshServerOptions({ hostKeyDir: "x" }).maxConnectionsPerIp).toBe(DEFAULT_MAX_CONNECTIONS_PER_IP);
  });

  it("keeps what it was given", () => {
    expect(resolveSshServerOptions({ hostKeyDir: "x", host: "10.0.0.1", port: 22, maxConnectionsPerIp: 1 })).toEqual({
      host: "10.0.0.1",
      port: 22,
      maxConnectionsPerIp: 1,
    });
  });

  it("reads the peer address a connection reports", () => {
    expect(peerFrom({ ip: "10.0.0.2", port: 5003 })).toEqual(["10.0.0.2", 5003]);
  });

  it("reports no peer where there is no address", () => {
    // Not exempt from the limit — the limiter counts it under its own bucket
    // — but there is nothing to tell a session about.
    expect(peerFrom(undefined)).toBeUndefined();
    expect(peerFrom({})).toBeUndefined();
    expect(peerFrom({ port: 5003 })).toBeUndefined();
  });

  it("supplies a port for an address that arrives without one", () => {
    expect(peerFrom({ ip: "10.0.0.2" })).toEqual(["10.0.0.2", 0]);
  });
});

describe("what the server refuses to start as", () => {
  it("will not accept any credential on a public bind", async () => {
    // The policy was ported and tested on its own; this is the server
    // actually refusing to come up.
    await expect(
      startSshServer(async (_reader, writer) => writer.close(), {
        host: "0.0.0.0",
        port: EPHEMERAL,
        hostKeyDir: join(scratch, "keys"),
      }),
    ).rejects.toThrow(PermissiveAuthError);
  });

  it("starts on a public bind with a validator", async () => {
    const server = await serve(async (_reader, writer) => writer.close(), {
      host: "0.0.0.0",
      credentialsValidator: () => true,
    });
    expect(server.port).toBeGreaterThan(0);
  });

  it("starts on a public bind with an explicit opt-in", async () => {
    const server = await serve(async (_reader, writer) => writer.close(), {
      host: "0.0.0.0",
      allowUnauthenticated: true,
    });
    expect(server.port).toBeGreaterThan(0);
  });

  it("does not generate a host key when it refuses", async () => {
    // A server that must not start should leave nothing behind.
    const directory = join(scratch, "unused-keys");
    await expect(
      startSshServer(async (_reader, writer) => writer.close(), {
        host: "0.0.0.0",
        port: EPHEMERAL,
        hostKeyDir: directory,
      }),
    ).rejects.toThrow(PermissiveAuthError);
    expect(() => rmSync(join(directory, "ssh_host_key"))).toThrow();
  });
});
