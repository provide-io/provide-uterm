//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type CliOptions, type DriverResult, LANGUAGE, runCli, SERVER_CAPABILITIES, USAGE } from "./index.ts";

/** A fetch that says yes and names the path it was asked for. */
const OK = (async (input: unknown) => {
  return new Response(JSON.stringify({ path: new URL(String(input)).pathname }), { status: 200 });
}) as unknown as typeof fetch;

/** Run the command line and read the one line it wrote. */
async function invoke(argv: string[], options: CliOptions = {}): Promise<{ code: number; line: unknown }> {
  const written: string[] = [];
  const code = await runCli(argv, { fetchImpl: OK, ...options, write: (line) => written.push(line) });
  expect(written).toHaveLength(1);
  return { code, line: JSON.parse(written[0] ?? "") };
}

/** A scenario file that is never read from disk. */
function scenarioText(scenario: unknown): (path: string) => Promise<string> {
  return async () => JSON.stringify(scenario);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the client role", () => {
  it("runs a scenario and writes one result", async () => {
    const { code, line } = await invoke(
      ["client", "--base-url", "http://127.0.0.1:9", "--token", "issued", "--scenario", "/s/010_health.json"],
      { readScenario: scenarioText({ id: "010_health", steps: [{ id: "health", action: "health" }] }) },
    );

    expect(code).toBe(0);
    expect(line).toMatchObject({
      scenario_id: "010_health",
      language: LANGUAGE,
      role: "client",
      status: "completed",
      steps: [{ id: "health", fields: { status: 200, ok: true, body: { path: "/api/health" }, error: null } }],
    });
  });

  it("reads the scenario off disk when nobody hands it one", async () => {
    const directory = await mkdtemp(join(tmpdir(), "uterm-conformance-"));
    const file = join(directory, "011_health.json");
    await writeFile(file, JSON.stringify({ id: "011_health", steps: [{ id: "health", action: "health" }] }), "utf8");

    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9", "--scenario", file]);

    expect(code).toBe(0);
    expect(line).toMatchObject({ scenario_id: "011_health", status: "completed" });
  });

  it("runs without a token, for a scenario that presents none", async () => {
    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9", "--scenario", "/s/x.json"], {
      readScenario: scenarioText({ id: "x", steps: [{ id: "health", action: "health", auth: "none" }] }),
    });

    expect(code).toBe(0);
    expect((line as DriverResult).status).toBe("completed");
  });

  it("exits zero for a scenario this language does not support", async () => {
    // Unsupported is a reported outcome, not a driver failure: the harness
    // records the cell and prints it.
    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9", "--scenario", "/s/x.json"], {
      readScenario: scenarioText({ id: "x", requires: ["rfb.raw"], steps: [{ id: "health", action: "health" }] }),
    });

    expect(code).toBe(0);
    expect((line as DriverResult).status).toBe("unsupported");
  });

  it("exits non-zero when the driver itself failed", async () => {
    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9", "--scenario", "/s/x.json"], {
      readScenario: scenarioText({ id: "x", steps: [{ id: "s", action: "teleport" }] }),
    });

    expect(code).toBe(1);
    expect((line as DriverResult).status).toBe("error");
  });
});

describe("a run the driver could not start", () => {
  it("names the scenario from its file when the file cannot be read", async () => {
    // The id matches the file name by convention, so a result for an
    // unreadable file still lands in the right cell of the matrix.
    const { code, line } = await invoke(
      ["client", "--base-url", "http://127.0.0.1:9", "--scenario", "/s/012_missing.json"],
      {
        readScenario: async () => {
          throw new Error("ENOENT: no such file");
        },
      },
    );

    expect(code).toBe(1);
    expect(line).toMatchObject({ scenario_id: "012_missing", status: "error", steps: [] });
    expect((line as DriverResult).error).toContain("ENOENT");
  });

  it("reports a scenario that is not JSON", async () => {
    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9", "--scenario", "/s/013_x.json"], {
      readScenario: async () => "not json at all",
    });

    expect(code).toBe(1);
    expect((line as DriverResult).status).toBe("error");
    expect((line as DriverResult).scenario_id).toBe("013_x");
  });

  it("names the scenario from its file when the scenario does not name itself", async () => {
    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9", "--scenario", "/s/014_x.json"], {
      readScenario: scenarioText({ steps: [{ id: "health", action: "health" }] }),
    });

    expect(code).toBe(0);
    expect((line as DriverResult).scenario_id).toBe("014_x");
  });

  it("reports a scenario with no steps at all", async () => {
    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9", "--scenario", "/s/015_x.json"], {
      readScenario: scenarioText({ id: "015_x" }),
    });

    expect(code).toBe(1);
    expect((line as DriverResult).status).toBe("error");
  });

  it("requires a scenario", async () => {
    const { code, line } = await invoke(["client", "--base-url", "http://127.0.0.1:9"]);

    expect(code).toBe(1);
    expect((line as DriverResult).error).toContain("--scenario");
    expect((line as DriverResult).scenario_id).toBe("");
  });

  it("requires a base URL, and still names the scenario", async () => {
    const { code, line } = await invoke(["client", "--scenario", "/s/016_x.json"]);

    expect(code).toBe(1);
    expect((line as DriverResult).error).toContain("--base-url");
    expect((line as DriverResult).scenario_id).toBe("016_x");
  });

  it("refuses an argument that is not a flag", async () => {
    const { code, line } = await invoke(["client", "run", "--scenario", "/s/x.json"]);

    expect(code).toBe(1);
    expect((line as DriverResult).error).toContain("run");
  });

  it("refuses a flag with no value", async () => {
    const { code, line } = await invoke(["client", "--base-url"]);

    expect(code).toBe(1);
    expect((line as DriverResult).error).toContain("--base-url");
  });
});

describe("the server role", () => {
  it("announces where it is listening and what token to present", async () => {
    let stop = () => {};
    const stopped = new Promise<void>((resolve) => {
      stop = resolve;
    });
    const written: string[] = [];
    const running = runCli(["serve", "--auth", "dev_token"], {
      write: (line) => written.push(line),
      until: () => stopped,
    });
    // The announcement is written before the shutdown is asked for, so it is
    // there to read while the server is still up.
    await vi.waitFor(() => expect(written).toHaveLength(1));

    const line = JSON.parse(written[0] ?? "") as Record<string, unknown>;
    expect(line.role).toBe("server");
    expect(line.language).toBe(LANGUAGE);
    expect(line.token).not.toBe("");
    expect(line.capabilities).toEqual([...SERVER_CAPABILITIES]);
    // An ephemeral port, reported rather than agreed: nothing in the harness
    // may name one.
    const port = Number(new URL(String(line.base_url)).port);
    expect(port).toBeGreaterThan(0);

    // And it is really listening, on that port, answering as itself.
    const health = await fetch(`${String(line.base_url)}/api/health`);
    expect(health.status).toBe(200);
    expect(((await health.json()) as { service: string }).service).toBe("uterm-server");

    stop();
    expect(await running).toBe(0);
  });

  it("says why it could not start rather than dying quietly", async () => {
    // The harness waits for a line; the only thing worse than a failed cell
    // is a hung one.
    const { code, line } = await invoke(["serve", "--auth", "none"]);

    expect(code).toBe(1);
    expect(line).toMatchObject({ role: "server", language: LANGUAGE, status: "error" });
    expect((line as { error: string }).error).toContain("removed for security reasons");
  });
});

describe("anything else", () => {
  it("refuses a role it does not have", async () => {
    const { code, line } = await invoke(["dance"]);

    expect(code).toBe(2);
    expect(line).toMatchObject({ language: LANGUAGE, status: "error" });
    expect((line as { error: string }).error).toContain(USAGE);
    expect((line as { error: string }).error).toContain("dance");
  });

  it("refuses being run with no role at all", async () => {
    const { code, line } = await invoke([]);

    expect(code).toBe(2);
    expect((line as { error: string }).error).toContain(USAGE);
  });

  it("writes its one line to standard output when nobody redirects it", async () => {
    const stdout = vi.spyOn(process.stdout, "write").mockReturnValue(true);

    const code = await runCli(["dance"]);

    expect(code).toBe(2);
    expect(stdout).toHaveBeenCalledTimes(1);
    const written = String(stdout.mock.calls[0]?.[0]);
    // One line, terminated: the harness reads the stream a line at a time.
    expect(written.endsWith("\n")).toBe(true);
    expect(JSON.parse(written)).toMatchObject({ status: "error" });
  });
});
