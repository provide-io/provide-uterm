//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The server role of the live driver.
 *
 * What matters here is the protocol's shape rather than the server's answers,
 * which `server/app.test.ts` holds to the reference: one line of JSON, an
 * ephemeral port reported rather than agreed, a token that is a real
 * credential, and a stop that happens when the harness asks for one.
 */

import { PassThrough } from "node:stream";
import { describe, expect, it, vi } from "vitest";
import { announcement, DEFAULT_AUTH_MODE, runServe, SERVER_CAPABILITIES, shutdownRequested } from "./serve.ts";

/** Start the server role and hand back the announcement plus a way to stop. */
async function started(argv: string[] = []) {
  const written: string[] = [];
  let stop = () => {};
  const stopped = new Promise<void>((resolve) => {
    stop = resolve;
  });
  const finished = runServe(argv, { write: (line) => written.push(line), until: () => stopped });
  // The announcement is written the moment the socket is bound.
  while (written.length === 0) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return { line: JSON.parse(written[0] ?? "") as Record<string, unknown>, written, stop, finished };
}

describe("announcing", () => {
  it("writes exactly one line, in the shape the harness reads", async () => {
    const { line, written, stop, finished } = await started();
    expect(written).toHaveLength(1);
    expect(line.role).toBe("server");
    expect(line.language).toBe("typescript");
    expect(line.capabilities).toEqual([]);
    stop();
    expect(await finished).toBe(0);
  });

  it("claims no capability it cannot serve", () => {
    // The protocol's names are about the hijack surfaces. Claiming one would
    // make a scenario that requires it run against a server that cannot
    // answer it, and the cell would fail for the wrong reason.
    expect(SERVER_CAPABILITIES).toEqual([]);
  });

  it("reports a port nobody chose in advance", async () => {
    const { line, stop, finished } = await started();
    expect(Number(new URL(String(line.base_url)).port)).toBeGreaterThan(0);
    stop();
    await finished;
  });

  it("runs in dev_token mode when no mode was named", async () => {
    const { line, stop, finished } = await started();
    expect(DEFAULT_AUTH_MODE).toBe("dev_token");
    expect(String(line.token)).not.toBe("");
    stop();
    await finished;
  });

  it("mints a token its own server verifies rather than recognises", async () => {
    // The whole reason dev_token is safe: the credential goes through the
    // production validator. A forged one must be refused by the same path.
    const { line, stop, finished } = await started(["--auth", "dev_token"]);
    const base = String(line.base_url);
    const good = await fetch(`${base}/api/sessions`, { headers: { Authorization: `Bearer ${String(line.token)}` } });
    expect(good.status).toBe(200);
    const forged = await fetch(`${base}/api/sessions`, { headers: { Authorization: "Bearer nope.nope.nope" } });
    expect(forged.status).toBe(401);
    stop();
    await finished;
  });

  it("has already brought the auto_start sessions up when it announces", async () => {
    // What `004_session_shape` cannot assert while it masks `lifecycle_state`,
    // asserted here instead: the default configuration flags `provide-shell`
    // `auto_start`, so the very first request the harness makes must find it
    // running. Before the announcement rather than after, so this is a fact
    // about the server and not about who won a race.
    const { line, stop, finished } = await started();
    const response = await fetch(`${String(line.base_url)}/api/sessions`, {
      headers: { Authorization: `Bearer ${String(line.token)}` },
    });
    const sessions = (await response.json()) as { session_id: string; lifecycle_state: string; connected: boolean }[];
    expect(sessions[0]?.session_id).toBe("provide-shell");
    expect(sessions[0]?.lifecycle_state).toBe("running");
    // And still not connected: the session is up, but this server binds no
    // transport a client could attach through, and it does not pretend to.
    expect(sessions[0]?.connected).toBe(false);
    stop();
    await finished;
  });

  it("mints nothing in a mode that has no stub identity provider", async () => {
    const { line, stop, finished } = await started(["--auth", "jwt"]);
    expect(line.token).toBe("");
    stop();
    await finished;
  });

  it("builds its line out of the server it actually bound", () => {
    const server = { host: "127.0.0.1", port: 1, baseUrl: "http://example", close: async () => {} };
    expect(JSON.parse(announcement(server, "t"))).toEqual({
      role: "server",
      language: "typescript",
      base_url: "http://example",
      token: "t",
      capabilities: [],
    });
  });
});

describe("refusing to start", () => {
  it("says why on stdout rather than dying quietly", async () => {
    // The harness waits for a line; the only thing worse than a failed cell
    // is a hung one.
    const written: string[] = [];
    const code = await runServe(["--auth", "none"], { write: (line) => written.push(line) });
    expect(code).toBe(1);
    expect(JSON.parse(written[0] ?? "")).toMatchObject({ role: "server", language: "typescript", status: "error" });
  });

  it("says why when the arguments themselves are wrong", async () => {
    const written: string[] = [];
    expect(await runServe(["--auth"], { write: (line) => written.push(line) })).toBe(1);
    expect((JSON.parse(written[0] ?? "") as { error: string }).error).toContain("--auth has no value");
  });

  it("says why when the failure was not an Error at all", async () => {
    const written: string[] = [];
    const code = await runServe([], {
      write: (line) => written.push(line),
      listen: () => Promise.reject("a string nobody wrapped"),
    });
    expect(code).toBe(1);
    expect((JSON.parse(written[0] ?? "") as { error: string }).error).toBe("a string nobody wrapped");
  });
});

describe("being asked to stop", () => {
  it("stops when its input ends, which is the harness asking politely", async () => {
    const input = new PassThrough();
    const waiting = shutdownRequested(input);
    input.end();
    await expect(waiting).resolves.toBeUndefined();
  });

  it("stops when the process is signalled, which is the harness insisting", async () => {
    const input = new PassThrough();
    const waiting = shutdownRequested(input);
    process.emit("SIGTERM");
    await expect(waiting).resolves.toBeUndefined();
    // And leaves nothing behind: a listener that outlived the wait would hold
    // the process open past the point it was asked to leave.
    expect(process.listenerCount("SIGTERM")).toBe(0);
    expect(process.listenerCount("SIGINT")).toBe(0);
  });

  it("stops on an interrupt too", async () => {
    const input = new PassThrough();
    const waiting = shutdownRequested(input);
    process.emit("SIGINT");
    await expect(waiting).resolves.toBeUndefined();
  });

  it("writes to standard output and waits on the process when nobody says otherwise", async () => {
    // Both defaults at once: the announcement goes to stdout, and the wait is
    // the real one — stdin ending or a signal, which is what the harness uses.
    const stdout = vi.spyOn(process.stdout, "write").mockReturnValue(true);
    const finished = runServe([]);
    await vi.waitFor(() => expect(stdout).toHaveBeenCalledTimes(1));
    const line = JSON.parse(String(stdout.mock.calls[0]?.[0])) as Record<string, unknown>;
    expect(line.role).toBe("server");
    process.emit("SIGTERM");
    expect(await finished).toBe(0);
    stdout.mockRestore();
  });

  it("closes the socket it opened", async () => {
    const { line, stop, finished } = await started();
    stop();
    expect(await finished).toBe(0);
    await expect(fetch(`${String(line.base_url)}/api/health`)).rejects.toThrow();
  });
});
