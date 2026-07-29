//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { ALLOWED_URL_SCHEMES, cleanSnapshot, MAX_PORT, MIN_PORT, trimTail, validateSessionCreate } from "./index.ts";

interface ToolsGolden {
  screen: string;
  configs: Array<{
    name: string;
    connector_type: string;
    url: string | null;
    port: number | null;
    host: string | null;
    rejection: Record<string, unknown> | null;
  }>;
  snapshots: Array<{
    name: string;
    snapshot: Record<string, unknown>;
    output: string;
    tail_lines: number | null;
    cleaned: Record<string, unknown>;
  }>;
}

const golden = loadGolden<ToolsGolden>("mcptools_golden.json");

describe("what a model may ask to start", () => {
  it.each(golden.configs)("$name", (record) => {
    const rejection = validateSessionCreate({
      connectorType: record.connector_type,
      url: record.url ?? undefined,
      port: record.port ?? undefined,
      host: record.host ?? undefined,
    });
    expect(rejection ?? null).toEqual(record.rejection);
  });

  it("refuses a connector nobody allows, exactly", () => {
    // `session_create` is how a model starts a process.
    for (const connectorType of ["carrier-pigeon", "SHELL", "", "exec"]) {
      expect(validateSessionCreate({ connectorType })).toMatchObject({ error: "invalid_connector_type" });
    }
  });

  it("takes a real TCP port and nothing else", () => {
    for (const port of [MIN_PORT, 22, MAX_PORT]) {
      expect(validateSessionCreate({ connectorType: "ssh", port })).toBeUndefined();
    }
    for (const port of [0, -1, MAX_PORT + 1, 999999]) {
      expect(validateSessionCreate({ connectorType: "ssh", port })).toMatchObject({ error: "invalid_port", port });
    }
  });

  it("refuses a scheme nobody vetted", () => {
    // So a model cannot ask a worker to open whatever it likes.
    for (const url of ["file:///etc/passwd", "javascript:alert(1)", "gopher://x", "data:text/html,x"]) {
      expect(validateSessionCreate({ connectorType: "websocket", url })).toMatchObject({
        error: "invalid_url_scheme",
      });
    }
  });

  it("names a missing scheme rather than reporting a blank one", () => {
    // So an operator can tell "no scheme" from "a scheme I have not heard of".
    expect(validateSessionCreate({ connectorType: "websocket", url: "feed.example/s" })).toMatchObject({
      error: "invalid_url_scheme",
      scheme: "<missing>",
    });
  });

  it("takes the schemes the reference takes", () => {
    for (const scheme of ALLOWED_URL_SCHEMES) {
      expect(validateSessionCreate({ connectorType: "websocket", url: `${scheme}://feed.example/s` })).toBeUndefined();
    }
    expect([...ALLOWED_URL_SCHEMES].sort()).toEqual(["http", "https", "ssh", "telnet", "ws", "wss"]);
  });

  it("checks the host inside a URL, not only the one beside it", () => {
    // A model that cannot pass an internal host beside the URL could
    // otherwise pass one within it and reach the same place.
    for (const url of [
      "ws://127.0.0.1/s",
      "ws://localhost/s",
      "ws://169.254.169.254/",
      "ws://2130706433/",
      "ws://10.0.0.5/s",
    ]) {
      expect(validateSessionCreate({ connectorType: "websocket", url })).toMatchObject({ error: "invalid_host" });
    }
    expect(validateSessionCreate({ connectorType: "websocket", url: "wss://feed.example/s" })).toBeUndefined();
  });

  it("names the host as it was written, not as a URL parser rewrites it", () => {
    // JavaScript's `URL` turns `ws://2130706433/` into `127.0.0.1`; Python
    // leaves it alone. Both refuse — the refusal should name what the caller
    // actually sent.
    expect(validateSessionCreate({ connectorType: "websocket", url: "ws://2130706433/" })).toMatchObject({
      host: "2130706433",
    });
    expect(validateSessionCreate({ connectorType: "websocket", url: "ws://0177.0.0.1/" })).toMatchObject({
      host: "0177.0.0.1",
    });
  });

  it("finds the host past credentials, a port and brackets", () => {
    expect(
      validateSessionCreate({ connectorType: "websocket", url: "ws://user:pw@127.0.0.1:8080/s" }), // pragma: allowlist secret
    ).toMatchObject({ host: "127.0.0.1" });
    expect(validateSessionCreate({ connectorType: "websocket", url: "ws://[::1]:8080/s" })).toMatchObject({
      host: "::1",
    });
  });

  it("takes a URL that names no host at all", () => {
    // There is nothing to refuse: the scheme passed, and the rest is the
    // worker's to make sense of.
    expect(validateSessionCreate({ connectorType: "websocket", url: "ws:///s" })).toBeUndefined();
  });

  it("checks a host given on its own", () => {
    for (const host of ["127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "2130706433"]) {
      expect(validateSessionCreate({ connectorType: "ssh", host })).toMatchObject({ error: "invalid_host", host });
    }
    expect(validateSessionCreate({ connectorType: "ssh", host: "shell.example" })).toBeUndefined();
  });

  it("reports the first thing wrong, in the reference's order", () => {
    // So a request wrong in more than one way reads the same on both sides.
    expect(validateSessionCreate({ connectorType: "nope", port: 0 })).toMatchObject({
      error: "invalid_connector_type",
    });
    expect(validateSessionCreate({ connectorType: "ssh", port: 0, url: "file:///x" })).toMatchObject({
      error: "invalid_port",
    });
    expect(validateSessionCreate({ connectorType: "ssh", url: "file:///x", host: "127.0.0.1" })).toMatchObject({
      error: "invalid_url_scheme",
    });
  });

  it("takes a request with everything named and nothing wrong", () => {
    expect(
      validateSessionCreate({ connectorType: "ssh", url: "ssh://shell.example", port: 22, host: "shell.example" }),
    ).toBeUndefined();
  });
});

describe("what a model is shown", () => {
  it.each(golden.snapshots)("$name", (record) => {
    expect(cleanSnapshot(record.snapshot, record.output, record.tail_lines ?? undefined)).toEqual(record.cleaned);
  });

  it("keeps escapes only in the raw shape", () => {
    // A model reading escape sequences is reading noise, and one being fed
    // them is a prompt-injection surface.
    const snapshot = { screen: golden.screen, cols: 80 };
    expect(String(cleanSnapshot(snapshot, "raw").screen)).toContain("[");
    expect(String(cleanSnapshot(snapshot, "text").screen)).not.toContain("[");
    expect(String(cleanSnapshot(snapshot, "rendered").screen)).not.toContain("[");
  });

  it("gives an unknown mode the filtered shape, not the raw one", () => {
    // The safe direction: a caller that misspells the mode gets less than it
    // asked for rather than everything.
    const cleaned = cleanSnapshot({ screen: golden.screen, cols: 80 }, "elsewhere");
    expect(String(cleaned.screen)).not.toContain("[");
    expect(cleaned.cols).toBe(80);
  });

  it("gives text the screen and nothing else", () => {
    expect(cleanSnapshot({ screen: "a", cursor: { x: 1 }, cols: 80, rows: 25 }, "text")).toEqual({ screen: "a" });
  });

  it("gives rendered the layout a model needs to read the grid", () => {
    expect(cleanSnapshot({ screen: "a", cursor: { x: 1 }, cols: 80, rows: 25 }, "rendered")).toEqual({
      screen: "a",
      cursor: { x: 1 },
      cols: 80,
      rows: 25,
    });
  });

  it("reports only the layout the snapshot had", () => {
    // Compared by key, not by value: a key present and undefined is not the
    // same as absent, and `toEqual` treats them alike.
    expect(Object.keys(cleanSnapshot({ screen: "a" }, "rendered"))).toEqual(["screen"]);
    expect(Object.keys(cleanSnapshot({ screen: "a", cols: 80 }, "rendered")).sort()).toEqual(["cols", "screen"]);
    expect(cleanSnapshot({ screen: "a", cols: 80 }, "rendered")).toEqual({ screen: "a", cols: 80 });
  });

  it("reads a scheme however it was capitalised", () => {
    // A shouted scheme is the same scheme.
    for (const url of ["WSS://feed.example/s", "WsS://feed.example/s", "SSH://shell.example"]) {
      expect(validateSessionCreate({ connectorType: "websocket", url })).toBeUndefined();
    }
  });

  it("hands raw back untouched when there is nothing to trim", () => {
    const snapshot = { screen: "a", cols: 80 };
    expect(cleanSnapshot(snapshot, "raw")).toEqual(snapshot);
  });

  it("takes a snapshot with no screen at all", () => {
    expect(cleanSnapshot({ cols: 80 }, "text")).toEqual({ screen: "" });
    expect(cleanSnapshot({ screen: 5 }, "text")).toEqual({ screen: "" });
  });
});

describe("trimming a screen to its last few lines", () => {
  it("keeps the last lines and not the first", () => {
    expect(trimTail("one\ntwo\nthree", 2)).toBe("two\nthree");
    expect(trimTail("one\ntwo\nthree", 1)).toBe("three");
  });

  it("leaves a screen shorter than the limit alone", () => {
    // Including its trailing newline, which splitting and rejoining would eat.
    expect(trimTail("one\ntwo\n", 5)).toBe("one\ntwo\n");
    expect(trimTail("one\ntwo\n", 2)).toBe("one\ntwo\n");
  });

  it("does nothing without a limit worth having", () => {
    for (const tail of [undefined, 0, -1]) {
      expect(trimTail("one\ntwo\nthree", tail)).toBe("one\ntwo\nthree");
    }
  });

  it("counts lines the way Python counts them", () => {
    // A trailing break ends the last line rather than starting another.
    expect(trimTail("one\ntwo\n", 1)).toBe("two");
    expect(trimTail("one\r\ntwo", 1)).toBe("two");
  });
});
