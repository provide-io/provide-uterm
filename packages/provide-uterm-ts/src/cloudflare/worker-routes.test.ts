//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  ASSET_PREFIX,
  DO_ROUTE_PATTERNS,
  extractWorkerId,
  HEALTH_PATH,
  publicRoute,
  resolveSpaRoute,
  STATIC_ASSET_PATTERN,
} from "./index.ts";

interface RoutesGolden {
  static_asset_pattern: string;
  do_route_patterns: string[];
  paths: Array<{
    name: string;
    path: string;
    health: boolean;
    asset: boolean;
    asset_name: string | null;
    static: boolean;
    static_name: string | null;
  }>;
  spa: Array<{ path: string; resolved: { kind: string; extra: Record<string, unknown> } | null }>;
  worker_ids: Array<{ path: string; worker_id: string | null }>;
}

const golden = loadGolden<RoutesGolden>("workerroutes_golden.json");

/**
 * A pattern's source in the reference's spelling.
 *
 * Two dialect differences and no behavioural one: JavaScript escapes the
 * separator in a source where Python does not, and writes a named group
 * `(?<n>)` where Python writes `(?P<n>)`. What each pattern actually *matches*
 * is pinned by the recorded paths; this only catches one being changed or
 * dropped.
 */
function asPythonSource(source: string): string {
  return source.replaceAll("\\/", "/").replaceAll("(?<", "(?P<");
}

describe("what is served without asking who is calling", () => {
  it.each(golden.paths)("$name", (record) => {
    const found = publicRoute(record.path);
    if (record.health) {
      expect(found).toEqual({ kind: "health" });
      return;
    }
    if (record.asset) {
      expect(found).toEqual({ kind: "asset", name: record.asset_name });
      return;
    }
    if (record.static) {
      expect(found).toEqual({ kind: "static", name: record.static_name });
      return;
    }
    // Not public is not the same as refused: it falls through to the routes
    // that do ask.
    expect(found).toBeUndefined();
  });

  it("serves only the three kinds of static file it means to", () => {
    // A closed set: a `.json` of configuration the build happened to emit is
    // not served by this route at all.
    for (const path of ["/index.html", "/theme.css", "/bundle.js"]) {
      expect(publicRoute(path)?.kind).toBe("static");
    }
    for (const path of ["/config.json", "/README", "/app.wasm", "/.env"]) {
      expect(publicRoute(path)).toBeUndefined();
    }
  });

  it("refuses a name outside the alphabet, encoded separators included", () => {
    // `%` is not in the set, so an encoded separator never reaches the asset
    // loader through this route.
    for (const path of ["/a%2Fb.js", "/my app.js", "/app.js?v=1", "/über.css"]) {
      expect(publicRoute(path)).toBeUndefined();
    }
  });

  it("matches the pattern the reference matches with", () => {
    // Compared in the reference's spelling — see `asPythonSource`.
    expect(asPythonSource(STATIC_ASSET_PATTERN.source)).toBe(golden.static_asset_pattern);
  });

  it("takes anything under the asset prefix, folders included", () => {
    expect(publicRoute("/assets/vendor/xterm.css")).toEqual({ kind: "asset", name: "vendor/xterm.css" });
    // Including a bare prefix, which the loader then has to answer for.
    expect(publicRoute(`${ASSET_PREFIX}`)).toEqual({ kind: "asset", name: "" });
  });

  it("answers the health path and nothing like it", () => {
    expect(publicRoute(HEALTH_PATH)).toEqual({ kind: "health" });
    expect(publicRoute("/api/health/")).toBeUndefined();
    expect(publicRoute("/api/healthz")).toBeUndefined();
  });
});

describe("which page a path names", () => {
  it.each(golden.spa)("$path", (record) => {
    const resolved = resolveSpaRoute(record.path);
    if (record.resolved === null) {
      expect(resolved).toBeUndefined();
      return;
    }
    expect(resolved).toEqual({
      kind: record.resolved.kind,
      ...(record.resolved.extra.session_id === undefined
        ? {}
        : { sessionId: record.resolved.extra.session_id, surface: record.resolved.extra.surface }),
    });
  });

  it("gives a session page a user surface and the rest an operator one", () => {
    // So a page cannot be reached with more of the interface than its kind
    // implies.
    expect(resolveSpaRoute("/app/session/sess-1")?.surface).toBe("user");
    for (const kind of ["inspect", "replay", "operator"]) {
      expect(resolveSpaRoute(`/app/${kind}/sess-1`)?.surface).toBe("operator");
    }
  });

  it("knows a share link, which is how somebody without an account arrives", () => {
    expect(resolveSpaRoute("/s/sess-1")).toEqual({ kind: "share", sessionId: "sess-1", surface: "user" });
  });

  it("does not know a page kind nobody defined", () => {
    for (const path of ["/app/hijack/sess-1", "/app/admin/sess-1", "/app/session/", "/app/session/a/b"]) {
      expect(resolveSpaRoute(path)).toBeUndefined();
    }
  });

  it("bounds a session id in a page route", () => {
    expect(resolveSpaRoute(`/app/session/${"a".repeat(64)}`)?.sessionId).toBe("a".repeat(64));
    expect(resolveSpaRoute(`/app/session/${"a".repeat(65)}`)).toBeUndefined();
    expect(resolveSpaRoute("/app/session/a.b")).toBeUndefined();
    expect(resolveSpaRoute("/s/a%2Fb")).toBeUndefined();
  });

  it("takes the dashboard at the three paths that mean it", () => {
    for (const path of ["/", "/app", "/app/"]) {
      expect(resolveSpaRoute(path)).toEqual({ kind: "dashboard" });
    }
    for (const path of ["/app/connect", "/app/connect/"]) {
      expect(resolveSpaRoute(path)).toEqual({ kind: "connect" });
    }
  });
});

describe("which session a proxied route names", () => {
  it.each(golden.worker_ids)("$path", (record) => {
    expect(extractWorkerId(record.path)).toBe(record.worker_id ?? undefined);
  });

  it("bounds the id, because it becomes an object name", () => {
    // A longer or differently-spelled string would name an object that a
    // filesystem or a KV key might read differently than this does.
    expect(extractWorkerId(`/tunnel/${"a".repeat(64)}`)).toBe("a".repeat(64));
    expect(extractWorkerId(`/tunnel/${"a".repeat(65)}`)).toBeUndefined();
    for (const path of ["/tunnel/a.b", "/tunnel/a%2Fb", "/tunnel/a/b", "/tunnel/"]) {
      expect(extractWorkerId(path)).toBeUndefined();
    }
  });

  it("needs the terminal suffix on a socket route", () => {
    expect(extractWorkerId("/ws/browser/sess-1/term")).toBe("sess-1");
    expect(extractWorkerId("/ws/browser/sess-1")).toBeUndefined();
  });

  it("takes anything after a hijack and nothing after the other two", () => {
    expect(extractWorkerId("/worker/sess-1/hijack")).toBe("sess-1");
    expect(extractWorkerId("/worker/sess-1/hijack/release")).toBe("sess-1");
    expect(extractWorkerId("/worker/sess-1/input_mode")).toBe("sess-1");
    expect(extractWorkerId("/worker/sess-1/disconnect_worker")).toBe("sess-1");
    expect(extractWorkerId("/worker/sess-1/input_mode/extra")).toBeUndefined();
    expect(extractWorkerId("/worker/sess-1/anything_else")).toBeUndefined();
  });

  it("routes by the patterns the reference routes by", () => {
    expect(DO_ROUTE_PATTERNS.map((pattern) => asPythonSource(pattern.source))).toEqual(golden.do_route_patterns);
  });

  it("names nothing for a path nobody proxies", () => {
    for (const path of ["/api/sessions/sess-1", "/nowhere", "/"]) {
      expect(extractWorkerId(path)).toBeUndefined();
    }
  });
});
