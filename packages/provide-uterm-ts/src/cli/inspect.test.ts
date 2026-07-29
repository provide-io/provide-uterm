//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  DEFAULT_INTERCEPT_TIMEOUT_ACTION,
  DEFAULT_INTERCEPT_TIMEOUT_S,
  type InspectArgs,
  inspectDisplayName,
  planInspect,
} from "./index.ts";

interface InspectGolden {
  cases: Array<{
    name: string;
    args: Record<string, unknown>;
    tunnel_info: Record<string, unknown>;
    ran: {
      created?: { server: string; display_name: string; token: string | null; target_port: number };
      inspect?: Record<string, unknown>;
    };
    stdout: string;
    stderr: string;
    exit_code: number | null;
  }>;
}

const golden = loadGolden<InspectGolden>("inspect_golden.json");

/** The corpus's arguments as the port takes them. */
function argsOf(record: Record<string, unknown>): InspectArgs {
  return {
    server: record.server as string,
    port: record.port as number,
    ...(record.listen_port === undefined ? {} : { listenPort: record.listen_port as number }),
    ...(record.display_name === undefined ? {} : { displayName: record.display_name as string }),
    ...(record.intercept === undefined ? {} : { intercept: record.intercept as boolean }),
    ...(record.intercept_timeout === undefined ? {} : { interceptTimeout: record.intercept_timeout as number }),
    ...(record.intercept_timeout_action === undefined
      ? {}
      : { interceptTimeoutAction: record.intercept_timeout_action as string }),
  };
}

describe("what inspecting decides", () => {
  it.each(golden.cases)("$name", (record) => {
    const plan = planInspect(argsOf(record.args), record.tunnel_info);
    expect(`${plan.output.join("\n")}\n`).toBe(record.stdout);

    if (record.exit_code !== null) {
      expect(plan.ok).toBe(false);
      expect(plan.ok === false && plan.exitCode).toBe(record.exit_code);
      expect(plan.ok === false && `${plan.error}\n`).toBe(record.stderr);
      return;
    }

    expect(plan.ok).toBe(true);
    if (plan.ok) {
      const ran = record.ran.inspect as Record<string, unknown>;
      expect(plan.wsEndpoint).toBe(ran.ws_endpoint);
      expect(plan.workerToken).toBe(ran.worker_token);
      expect(plan.targetPort).toBe(ran.target_port);
      expect(plan.listenPort).toBe(ran.listen_port);
      expect(plan.intercept).toBe(ran.intercept);
      expect(plan.interceptTimeout).toBe(ran.intercept_timeout);
      expect(plan.interceptTimeoutAction).toBe(ran.intercept_timeout_action);
      expect(plan.displayName).toBe(record.ran.created?.display_name);
    }
  });

  it("names a session after the port when nobody named it", () => {
    // A list of shares all called the same thing is a list nobody can read.
    expect(inspectDisplayName({ server: "s", port: 8080 })).toBe("http:8080");
    expect(inspectDisplayName({ server: "s", port: 3000 })).toBe("http:3000");
    expect(inspectDisplayName({ server: "s", port: 8080, displayName: "" })).toBe("http:8080");
    expect(inspectDisplayName({ server: "s", port: 8080, displayName: "my api" })).toBe("my api");
  });

  it("says the tunnel was created before it checks the answer", () => {
    // The tunnel exists either way, and saying so is what tells an operator
    // how far it got.
    const plan = planInspect({ server: "https://x", port: 80 }, { tunnel_id: "t-1" });
    expect(plan.ok).toBe(false);
    expect(plan.output[0]).toBe("Creating tunnel... done (t-1)");
  });

  it("names the tunnel however the server named it", () => {
    // A server calling it a session and one calling it a tunnel are both
    // naming what was just created.
    for (const info of [{ tunnel_id: "t-1" }, { session_id: "t-1" }]) {
      expect(planInspect({ server: "https://x", port: 80 }, info).output[0]).toBe("Creating tunnel... done (t-1)");
    }
    expect(planInspect({ server: "https://x", port: 80 }, {}).output[0]).toBe("Creating tunnel... done");
  });

  it("takes an empty name as a name, not as a missing one", () => {
    // The reference reads the field with a default, so it falls back only
    // when the field is absent — a server that sent an empty `tunnel_id` has
    // named the tunnel as nothing rather than deferred to `session_id`.
    expect(planInspect({ server: "https://x", port: 80 }, { tunnel_id: "", session_id: "s-9" }).output[0]).toBe(
      "Creating tunnel... done",
    );
    expect(planInspect({ server: "https://x", port: 80 }, { session_id: "s-9" }).output[0]).toBe(
      "Creating tunnel... done (s-9)",
    );
  });

  it("refuses an answer with nowhere to connect to", () => {
    for (const info of [{}, { ws_endpoint: "" }, { ws_endpoint: 42 }]) {
      const plan = planInspect({ server: "https://x", port: 80 }, info);
      expect(plan.ok).toBe(false);
      expect(plan.ok === false && plan.error).toBe("error: server response missing ws_endpoint");
    }
  });

  it("carries a shared session over TLS when the server was", () => {
    // A share that silently drops to cleartext is worse than one that fails.
    const secure = planInspect({ server: "https://warp.example", port: 80 }, { ws_endpoint: "/t" });
    expect(secure.ok && secure.wsEndpoint).toBe("wss://warp.example/t");
    const plain = planInspect({ server: "http://warp.example", port: 80 }, { ws_endpoint: "/t" });
    expect(plain.ok && plain.wsEndpoint).toBe("ws://warp.example/t");
  });

  it("takes an absolute endpoint as it stands", () => {
    const plan = planInspect({ server: "https://warp.example", port: 80 }, { ws_endpoint: "wss://elsewhere/t" });
    expect(plan.ok && plan.wsEndpoint).toBe("wss://elsewhere/t");
  });

  it("says nothing about a share link the server did not give", () => {
    const plan = planInspect({ server: "https://x", port: 80 }, { ws_endpoint: "/t" });
    expect(plan.ok && plan.output.some((line) => line.includes("Share:"))).toBe(false);
  });

  it("says when requests are being paused, and on what terms", () => {
    // A proxy that pauses requests looks exactly like one that has hung.
    const plan = planInspect(
      { server: "https://x", port: 80, intercept: true, interceptTimeout: 5, interceptTimeoutAction: "drop" },
      { ws_endpoint: "/t" },
    );
    expect(plan.ok && plan.output.some((line) => line === "  Intercept: ON (timeout: 5.0s, action: drop)")).toBe(true);
  });

  it("turns interception on for anything that reads as on", () => {
    // The reference reads the flag and then asks whether it is true, so a
    // caller passing 1 or a non-empty string has turned it on.
    for (const value of [true, 1, "yes", {}]) {
      const plan = planInspect({ server: "https://x", port: 80, intercept: value }, { ws_endpoint: "/t" });
      expect(plan.ok && plan.intercept).toBe(true);
    }
    for (const value of [false, 0, "", null, undefined]) {
      const plan = planInspect({ server: "https://x", port: 80, intercept: value }, { ws_endpoint: "/t" });
      expect(plan.ok && plan.intercept).toBe(false);
    }
  });

  it("says nothing about interception when it is off", () => {
    const plan = planInspect({ server: "https://x", port: 80 }, { ws_endpoint: "/t" });
    expect(plan.ok && plan.output.some((line) => line.includes("Intercept"))).toBe(false);
  });

  it("uses the intercept defaults the reference uses", () => {
    expect(DEFAULT_INTERCEPT_TIMEOUT_S).toBe(30);
    expect(DEFAULT_INTERCEPT_TIMEOUT_ACTION).toBe("forward");
    const plan = planInspect({ server: "https://x", port: 80, intercept: true }, { ws_endpoint: "/t" });
    expect(plan.ok && plan.output.some((line) => line === "  Intercept: ON (timeout: 30.0s, action: forward)")).toBe(
      true,
    );
  });

  it("renders a whole-numbered timeout as the float it is", () => {
    // So a line read in a transcript is the same line on both runtimes.
    for (const [timeout, shown] of [
      [30, "30.0"],
      [5, "5.0"],
      [2.5, "2.5"],
      [0.5, "0.5"],
    ] as const) {
      const plan = planInspect(
        { server: "https://x", port: 80, intercept: true, interceptTimeout: timeout },
        { ws_endpoint: "/t" },
      );
      expect(plan.ok && plan.output.some((line) => line.includes(`timeout: ${shown}s`))).toBe(true);
    }
  });

  it("listens wherever it was told, or wherever is free", () => {
    const chosen = planInspect({ server: "https://x", port: 80, listenPort: 9000 }, { ws_endpoint: "/t" });
    expect(chosen.ok && chosen.listenPort).toBe(9000);
    const any = planInspect({ server: "https://x", port: 80 }, { ws_endpoint: "/t" });
    expect(any.ok && any.listenPort).toBe(0);
  });

  it("names the port it is inspecting, which is not the one it listens on", () => {
    const plan = planInspect({ server: "https://x", port: 8080, listenPort: 9000 }, { ws_endpoint: "/t" });
    expect(plan.ok && plan.targetPort).toBe(8080);
    expect(plan.ok && plan.output.some((line) => line.includes("localhost:8080"))).toBe(true);
  });

  it("ends its output with how to stop", () => {
    // Somebody who has just started a proxy needs to know how to end it.
    const plan = planInspect({ server: "https://x", port: 80 }, { ws_endpoint: "/t" });
    expect(plan.output.at(-1)).toBe("Press Ctrl+C to stop.\n");
  });
});
