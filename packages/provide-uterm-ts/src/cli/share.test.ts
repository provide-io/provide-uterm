//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type BridgeSource,
  bridgeLoop,
  planShare,
  READ_SIZE,
  resolveDisplayName,
  resolveToken,
  resolveWsEndpoint,
  type ShareArgs,
  type ShareEnvironment,
} from "./index.ts";

interface ShareGolden {
  tokens: Array<{
    name: string;
    token: string | null;
    token_file: string | null;
    file_contents: Record<string, string>;
    resolved: string | null;
  }>;
  display_names: Array<{
    name: string;
    display_name: string | null;
    user: string;
    node: string;
    resolved: string;
  }>;
  commands: Array<{
    name: string;
    server: string;
    attach: boolean;
    cmd: string[] | null;
    tunnel_info: Record<string, unknown>;
    log: Array<Array<unknown>>;
    ran: { ws_endpoint?: string; worker_token?: string; attach?: boolean };
    stdout: string;
    stderr: string;
    exit_code: number | null;
  }>;
  bridges: Array<{
    name: string;
    reads: Array<string | null>;
    receives: Array<string | null>;
    is_attach: boolean;
    log: Array<Array<unknown>>;
  }>;
}

const golden = loadGolden<ShareGolden>("share_golden.json");

/** The surroundings the corpus ran against. */
function environmentFor(
  files: Record<string, string>,
  user: string | "raises" = "ada",
  node = "workstation",
): ShareEnvironment {
  return {
    readTokenFile: (path) => files[path],
    defaultTokenFile: "/home/ada/.uterm/token",
    getUser: () => {
      if (user === "raises") {
        throw new Error("no such user");
      }
      return user;
    },
    hostname: () => node,
  };
}

describe("where the bearer token comes from", () => {
  it.each(golden.tokens)("$name", (record) => {
    // The corpus names each path rather than quoting it: the reference ran
    // against a temporary directory, and a corpus holding that path could
    // never be checked for drift.
    const environment = environmentFor(record.file_contents);
    const args: ShareArgs = {
      server: "https://warp.example",
      ...(record.token === null ? {} : { token: record.token }),
      ...(record.token_file === null ? {} : { tokenFile: record.token_file }),
    };
    expect(resolveToken(args, environment) ?? null).toBe(record.resolved);
  });

  it("lets a flag beat a file", () => {
    // So a stale credential on disk can be overridden without deleting it.
    const environment = environmentFor({ "/tmp/t": "from-file" });
    expect(resolveToken({ server: "s", token: "from-flag", tokenFile: "/tmp/t" }, environment)).toBe("from-flag");
  });

  it("does not let an empty flag beat a file", () => {
    // The reference tests the value for truth, so `--token ""` is not an
    // instruction to share without one.
    const environment = environmentFor({ "/tmp/t": "from-file" });
    expect(resolveToken({ server: "s", token: "", tokenFile: "/tmp/t" }, environment)).toBe("from-file");
  });

  it("falls back to the default file when nobody named one", () => {
    const environment = environmentFor({ "/home/ada/.uterm/token": "from-default" });
    expect(resolveToken({ server: "s" }, environment)).toBe("from-default");
  });

  it("trims what it reads, since a file ends with a newline", () => {
    const environment = environmentFor({ "/tmp/t": "  tok  \n" });
    expect(resolveToken({ server: "s", tokenFile: "/tmp/t" }, environment)).toBe("tok");
  });

  it("tells a file holding nothing from a file that is not there", () => {
    // Both are falsy to whoever asks, but they are not the same answer.
    expect(resolveToken({ server: "s", tokenFile: "/tmp/t" }, environmentFor({ "/tmp/t": "   \n" }))).toBe("");
    expect(resolveToken({ server: "s", tokenFile: "/tmp/t" }, environmentFor({}))).toBeUndefined();
  });

  it("shares without a token when there is none anywhere", () => {
    expect(resolveToken({ server: "s" }, environmentFor({}))).toBeUndefined();
  });
});

describe("what the session is called", () => {
  it.each(golden.display_names)("$name", (record) => {
    const environment = environmentFor({}, record.user, record.node);
    expect(
      resolveDisplayName(
        { server: "s", ...(record.display_name === null ? {} : { displayName: record.display_name }) },
        environment,
      ),
    ).toBe(record.resolved);
  });

  it("uses the name given rather than working one out", () => {
    expect(resolveDisplayName({ server: "s", displayName: "my session" }, environmentFor({}))).toBe("my session");
  });

  it("still labels a session when the user cannot be named", () => {
    expect(resolveDisplayName({ server: "s" }, environmentFor({}, "raises"))).toBe("unknown@workstation");
  });

  it("still labels a session when the host cannot be named", () => {
    expect(resolveDisplayName({ server: "s" }, environmentFor({}, "ada", ""))).toBe("ada@localhost");
  });
});

describe("where the websocket is", () => {
  it("upgrades the scheme when joining a path to its server", () => {
    // A session shared over TLS must be carried over TLS: a share that
    // silently drops to cleartext is worse than one that fails.
    expect(resolveWsEndpoint("https://warp.example", "/tunnel/abc")).toBe("wss://warp.example/tunnel/abc");
    expect(resolveWsEndpoint("http://warp.example", "/tunnel/abc")).toBe("ws://warp.example/tunnel/abc");
  });

  it("takes an absolute endpoint as it stands", () => {
    // The server may put the tunnel somewhere else entirely.
    for (const endpoint of ["wss://elsewhere.example/t", "ws://elsewhere.example/t"]) {
      expect(resolveWsEndpoint("https://warp.example", endpoint)).toBe(endpoint);
    }
  });

  it("does not double the slash between server and path", () => {
    expect(resolveWsEndpoint("https://warp.example/", "/tunnel/abc")).toBe("wss://warp.example/tunnel/abc");
    expect(resolveWsEndpoint("https://warp.example///", "/tunnel/abc")).toBe("wss://warp.example/tunnel/abc");
  });

  it("rewrites a scheme wherever it appears, as the reference does", () => {
    // The reference replaces unanchored, so a server whose path mentions
    // `http://` has that rewritten too. Carried over: a port that resolved a
    // different URL would be the worse answer.
    expect(resolveWsEndpoint("https://warp.example/http://x", "/t")).toBe("wss://warp.example/ws://x/t");
  });
});

describe("what sharing a session decides", () => {
  it.each(golden.commands)("$name", (record) => {
    const plan = planShare(
      {
        server: record.server,
        token: "t",
        displayName: "d",
        attach: record.attach,
        ...(record.cmd === null ? {} : { cmd: record.cmd }),
      },
      record.tunnel_info,
      environmentFor({}),
    );
    if (record.exit_code !== null) {
      expect(plan.ok).toBe(false);
      expect(plan.ok === false && plan.exitCode).toBe(record.exit_code);
      expect(plan.ok === false && `${plan.error}\n`).toBe(record.stderr);
      return;
    }
    expect(plan.ok).toBe(true);
    if (plan.ok) {
      expect(plan.wsEndpoint).toBe(record.ran.ws_endpoint);
      expect(plan.workerToken).toBe(record.ran.worker_token);
      expect(plan.attach).toBe(record.ran.attach);
      expect(`${plan.output.join("\n")}\n`).toBe(record.stdout);
    }
  });

  it("prints the two urls the caller needs, and says which is which", () => {
    // One is for watching and one is for typing; handing over the wrong one
    // hands over control of the terminal.
    const plan = planShare(
      { server: "https://warp.example" },
      golden.commands[0]?.tunnel_info ?? {},
      environmentFor({}),
    );
    expect(plan.ok).toBe(true);
    if (plan.ok) {
      expect(plan.output.some((line) => line.includes("View:"))).toBe(true);
      expect(plan.output.some((line) => line.includes("Control:"))).toBe(true);
    }
  });

  it("refuses an answer with nowhere to connect to", () => {
    // And refuses before printing, so it never advertises a share that was
    // never established.
    for (const info of [{}, { ws_endpoint: "" }, { ws_endpoint: 42 }, { share_url: "https://x" }]) {
      const plan = planShare({ server: "https://warp.example" }, info, environmentFor({}));
      expect(plan).toEqual({ ok: false, error: "error: server response missing ws_endpoint", exitCode: 1 });
    }
  });

  it("prints an empty url rather than the word undefined", () => {
    // A server that answered without one is a server bug; printing
    // "undefined" as a URL would look like a URL.
    const plan = planShare({ server: "https://x" }, { ws_endpoint: "/t" }, environmentFor({}));
    expect(plan.ok).toBe(true);
    if (plan.ok) {
      expect(plan.output).toContain("  View:    ");
      expect(plan.workerToken).toBe("");
    }
  });
});

describe("carrying bytes both ways", () => {
  /** A terminal handing over a script of reads and recording what it is given. */
  function sourceFor(log: Array<Array<unknown>>, reads: Array<string | null>): BridgeSource {
    const pending = [...reads];
    return {
      read: async (size) => {
        if (pending.length === 0) {
          throw Object.assign(new Error("nothing left"), { name: "EOFError" });
        }
        const value = pending.shift() as string | null;
        log.push(["read", size, value]);
        if (value === null) {
          throw Object.assign(new Error("the pty went away"), { code: "EIO" });
        }
        return new TextEncoder().encode(value);
      },
      write: async (data) => {
        log.push(["write", new TextDecoder().decode(data)]);
      },
      writeLocal: async (data) => {
        log.push(["write_local", new TextDecoder().decode(data)]);
      },
    };
  }

  /**
   * The events of one direction, in order.
   *
   * The two directions are compared separately because their *interleaving*
   * is not a property of the bridge. In the reference none of the scripted
   * coroutines ever suspends, so `gather` runs one direction to completion
   * before starting the other; here every `await` yields a microtask, so they
   * take turns. Both are correct, and neither is a contract. What is a
   * contract is that each direction reads, frames and writes in order, and
   * stops when its own side closes.
   */
  function direction(log: Array<Array<unknown>>, kinds: string[]): Array<Array<unknown>> {
    return log.filter(([kind]) => kinds.includes(kind as string));
  }

  it.each(golden.bridges)("$name", async (record) => {
    const log: Array<Array<unknown>> = [];
    const pending = [...record.receives];
    await bridgeLoop(
      sourceFor(log, record.reads),
      async (frame) => {
        log.push(["send", [...frame].map((byte) => byte.toString(16).padStart(2, "0")).join("")]);
      },
      async () => {
        if (pending.length === 0) {
          throw Object.assign(new Error("nothing left"), { name: "EOFError" });
        }
        const value = pending.shift() as string | null;
        log.push(["recv", value]);
        if (value === null) {
          throw Object.assign(new Error("the socket went away"), { code: "EPIPE" });
        }
        return new TextEncoder().encode(value);
      },
      { isAttach: record.is_attach },
    );
    expect(direction(log, ["read", "send"])).toEqual(direction(record.log, ["read", "send"]));
    expect(direction(log, ["recv", "write", "write_local"])).toEqual(
      direction(record.log, ["recv", "write", "write_local"]),
    );
    expect(log.length).toBe(record.log.length);
  });

  it("asks for the same bite the reference asks for", () => {
    expect(READ_SIZE).toBe(4096);
  });

  it("frames what it sends, rather than sending raw bytes", () => {
    // The far end demultiplexes by channel; unframed bytes would be read as a
    // channel and a flag.
    const log: Array<Array<unknown>> = [];
    return bridgeLoop(
      sourceFor(log, ["hi", ""]),
      async (frame) => {
        expect([...frame.slice(0, 2)]).toEqual([1, 0]);
        expect(new TextDecoder().decode(frame.slice(2))).toBe("hi");
      },
      async () => new Uint8Array(),
    );
  });

  it("writes to the caller's own terminal only when attached", async () => {
    for (const isAttach of [true, false]) {
      const log: Array<Array<unknown>> = [];
      const pending: Array<string | null> = ["there", ""];
      await bridgeLoop(
        sourceFor(log, [""]),
        async () => {},
        async () => new TextEncoder().encode(pending.shift() ?? ""),
        { isAttach },
      );
      expect(log.some(([kind]) => kind === (isAttach ? "write_local" : "write"))).toBe(true);
      expect(log.some(([kind]) => kind === (isAttach ? "write" : "write_local"))).toBe(false);
    }
  });

  it("ends when a side hangs up rather than raising", async () => {
    // A closed pty or socket is how a share ends, not a fault to report.
    const log: Array<Array<unknown>> = [];
    await expect(
      bridgeLoop(
        sourceFor(log, [null]),
        async () => {},
        async () => new Uint8Array(),
      ),
    ).resolves.toBeUndefined();
  });

  it("lets a real fault through rather than ending quietly", async () => {
    // A share that stopped for a programming error should say so, not look
    // like a peer that hung up.
    const source = sourceFor([], []);
    await expect(
      bridgeLoop(
        {
          ...source,
          read: async () => {
            throw new TypeError("a bug in the caller");
          },
        },
        async () => {},
        async () => new Uint8Array(),
      ),
    ).rejects.toThrow(TypeError);
  });

  it("lets a fault on the receiving side through as well", async () => {
    // Both directions treat a hang-up alike, and a bug alike.
    const source = sourceFor([], [""]);
    await expect(
      bridgeLoop(
        source,
        async () => {},
        async () => {
          throw new TypeError("a bug on the socket side");
        },
      ),
    ).rejects.toThrow(TypeError);
  });

  it("does not mistake a thrown non-error for a hang-up", async () => {
    // This runtime can throw anything at all; only a socket or a pty going
    // away ends a share quietly.
    const source = sourceFor([], []);
    await expect(
      bridgeLoop(
        {
          ...source,
          read: async () => {
            throw "not an error";
          },
        },
        async () => {},
        async () => new Uint8Array(),
      ),
    ).rejects.toBe("not an error");
  });

  it("carries both ways at once, not one after the other", async () => {
    // Running the directions in sequence would leave input from the far end
    // waiting until the terminal fell silent — which on a live session is
    // never.
    const log: Array<Array<unknown>> = [];
    const outgoing = ["a", "b", "c", ""];
    const incoming = ["x", ""];
    await bridgeLoop(
      sourceFor(log, outgoing),
      async () => {},
      async () => new TextEncoder().encode(incoming.shift() ?? ""),
      {},
    );
    const lastRead = log.findLastIndex(([kind]) => kind === "read");
    const firstWrite = log.findIndex(([kind]) => kind === "write");
    expect(firstWrite).toBeGreaterThanOrEqual(0);
    expect(firstWrite).toBeLessThan(lastRead);
  });

  it("ends quietly when a side simply stops answering", async () => {
    // Exhausting the script raises end-of-file, which is a share ending
    // rather than a fault — and is not a coded system error, so it has to be
    // recognised by name.
    const log: Array<Array<unknown>> = [];
    await expect(
      bridgeLoop(
        sourceFor(log, []),
        async () => {},
        async () => new Uint8Array(),
      ),
    ).resolves.toBeUndefined();
  });

  it("does not take anything merely carrying a code for a hang-up", async () => {
    // The reference catches exceptions, not shapes. A plain object with a
    // `code` is something a caller threw, and swallowing it would end the
    // share silently on a bug.
    const source = sourceFor([], []);
    await expect(
      bridgeLoop(
        {
          ...source,
          read: async () => {
            throw { code: "EIO", message: "not an error" };
          },
        },
        async () => {},
        async () => new Uint8Array(),
      ),
    ).rejects.toEqual({ code: "EIO", message: "not an error" });
  });

  it("keeps carrying input after the terminal has stopped producing", async () => {
    // Both directions run at once: a quiet terminal must not strand data
    // still arriving from the far end.
    const log: Array<Array<unknown>> = [];
    const pending: Array<string | null> = ["a", "b", ""];
    await bridgeLoop(
      sourceFor(log, [""]),
      async () => {},
      async () => new TextEncoder().encode(pending.shift() ?? ""),
    );
    expect(log.filter(([kind]) => kind === "write").map(([, value]) => value)).toEqual(["a", "b"]);
  });
});
