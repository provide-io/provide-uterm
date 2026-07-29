//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { extractTunnelId } from "./index.ts";

interface WatchGolden {
  extracted: Array<{ name: string; value: string; id: string }>;
}

const golden = loadGolden<WatchGolden>("watch_golden.json");

describe("reading the tunnel out of what somebody pasted", () => {
  it.each(golden.extracted)("$name", (record) => {
    expect(extractTunnelId(record.value)).toBe(record.id);
  });

  it("takes a bare identifier as it stands", () => {
    // Somebody who typed an identifier meant that identifier.
    for (const value of ["t-abc123", "t_abc_123", "abc", ""]) {
      expect(extractTunnelId(value)).toBe(value);
    }
  });

  it("reads it out of every link a person is given", () => {
    for (const path of ["app/inspect", "app/session", "app/operator", "s"]) {
      expect(extractTunnelId(`https://warp.example/${path}/t-abc123`)).toBe("t-abc123");
    }
  });

  it("does not care which scheme the link uses", () => {
    for (const scheme of ["https", "http", "wss", "ws"]) {
      expect(extractTunnelId(`${scheme}://warp.example/s/t-abc123`)).toBe("t-abc123");
    }
  });

  it("ignores anything a query says", () => {
    // A tunnel named in a query parameter is not the tunnel the path names,
    // and taking it would watch somebody else's session. The path wins where
    // there is one...
    expect(extractTunnelId("https://warp.example/s/t-abc123?id=t-other")).toBe("t-abc123");
    // ...and where there is not, a query that looks like a route is still not
    // one. This is the case that matters: a link whose *path* names nothing
    // and whose query names a tunnel must not be followed to it.
    for (const value of [
      "https://warp.example/?next=/s/t-other",
      "https://warp.example/redirect?to=/app/inspect/t-other",
      "https://warp.example/?id=t-other",
    ]) {
      expect(extractTunnelId(value)).toBe(value);
    }
  });

  it("stops at whatever ends the identifier", () => {
    for (const suffix of ["#top", "/more", "?x=1", ".json"]) {
      expect(extractTunnelId(`https://warp.example/s/t-abc123${suffix}`)).toBe("t-abc123");
    }
  });

  it("takes the first of two, not the last", () => {
    // A link cannot name two tunnels; the first is the one the route names.
    expect(extractTunnelId("https://warp.example/s/first/s/second")).toBe("first");
  });

  it("looks past a host with a port and a path in front", () => {
    expect(extractTunnelId("https://warp.example:8443/s/t-abc123")).toBe("t-abc123");
    expect(extractTunnelId("https://warp.example/tunnels/s/t-abc123")).toBe("t-abc123");
  });

  it("hands back an address naming no tunnel whole", () => {
    // Rather than empty: what was passed is then treated as the identifier,
    // and the server refuses it by name instead of the command failing with
    // nothing to say.
    for (const value of [
      "https://warp.example/",
      "https://warp.example/app/other/t-abc123",
      "https://warp.example/s/",
    ]) {
      expect(extractTunnelId(value)).toBe(value);
    }
  });

  it("only searches something that looks like an address", () => {
    // A bare path is not one, so it is taken as the identifier it may well be.
    for (const value of ["/s/t-abc123", "s/t-abc123"]) {
      expect(extractTunnelId(value)).toBe(value);
    }
  });

  it("searches anything holding a scheme marker, as the reference does", () => {
    // The test is the marker, not a real scheme — carried over rather than
    // tightened, since anything holding one is a caller's typo either way.
    expect(extractTunnelId("not-a-url://but-has-one")).toBe("not-a-url://but-has-one");
    expect(extractTunnelId("nonsense://x/s/t-abc")).toBe("t-abc");
  });

  it("takes an identifier of one character", () => {
    expect(extractTunnelId("https://warp.example/s/a")).toBe("a");
  });

  it("stops at a dot, which is not part of an identifier", () => {
    expect(extractTunnelId("https://warp.example/s/t.abc")).toBe("t");
  });
});
