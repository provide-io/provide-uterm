//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The driver's command line.
 *
 * `conformance/live/PROTOCOL.md` gives a driver two subcommands and one
 * promise: exactly one line of JSON on standard output, whatever happened.
 * The harness reads that line, so a driver that printed twice, or that
 * printed nothing on a bad argument, would be a cell the matrix cannot read.
 *
 * Both roles are real. `serve` stands this port's own server up on an
 * ephemeral port and keeps it there; `client` runs a scenario's steps and
 * writes down what it saw. Neither judges anything: every expectation belongs
 * to the harness, in one implementation, so four languages cannot disagree
 * about what an expectation *means* — only about what their server did.
 */

import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import { CLIENT_CAPABILITIES, type DriverResult, LANGUAGE, runClientScenario, type Scenario } from "./client-driver.ts";
import { parseFlags, required, USAGE } from "./flags.ts";
import { runServe, type ServeOptions } from "./serve.ts";
import { errorMessage } from "./transport.ts";

/** What the command line is run with. Defaults are the real thing. */
export interface CliOptions extends ServeOptions {
  /** Where the one line goes. */
  write?: ((line: string) => void) | undefined;
  /** The fetch the driver's transport uses. */
  fetchImpl?: typeof fetch | undefined;
  /** How a scenario file is read. */
  readScenario?: ((path: string) => Promise<string>) | undefined;
}

export { USAGE } from "./flags.ts";

/**
 * Run one subcommand and return the exit code.
 *
 * Zero when the driver did its job — including a scenario that was
 * unsupported, which is a reported outcome rather than a failure. One when
 * the driver itself failed, two when it was asked for something it has no
 * subcommand for.
 */
export async function runCli(argv: string[], options: CliOptions = {}): Promise<number> {
  const write =
    options.write ??
    ((line: string) => {
      process.stdout.write(`${line}\n`);
    });
  const [role, ...rest] = argv;

  if (role === "serve") {
    return runServe(rest, { ...options, write });
  }
  if (role !== "client") {
    write(
      JSON.stringify({
        role: null,
        language: LANGUAGE,
        status: "error",
        error: `unknown role ${JSON.stringify(role ?? null)}; ${USAGE}`,
      }),
    );
    return 2;
  }

  const result = await runClient(rest, options);
  write(JSON.stringify(result));
  return result.status === "error" ? 1 : 0;
}

/** Run the client role, turning anything that went wrong into a result. */
async function runClient(argv: string[], options: CliOptions): Promise<DriverResult> {
  // Held outside the try so a scenario that could not be read is still filed
  // under the right id: the id matches the file name by convention, which is
  // the only name available when the file itself is unreadable.
  let scenarioId = "";
  try {
    const flags = parseFlags(argv);
    const file = required(flags, "scenario");
    scenarioId = basename(file, ".json");
    const baseUrl = required(flags, "base-url");
    const read = options.readScenario ?? ((path: string) => readFile(path, "utf8"));
    const scenario = JSON.parse(await read(file)) as Scenario;
    if (typeof scenario.id === "string") {
      scenarioId = scenario.id;
    }
    return await runClientScenario(scenario, {
      scenarioId,
      baseUrl,
      // A scenario whose every step is unauthenticated needs no token, and a
      // harness that omits one should not be a usage error.
      token: flags.get("token") ?? "",
      fetchImpl: options.fetchImpl,
    });
  } catch (error) {
    return {
      scenario_id: scenarioId,
      language: LANGUAGE,
      role: "client",
      status: "error",
      capabilities: [...CLIENT_CAPABILITIES],
      steps: [],
      error: errorMessage(error),
    };
  }
}
