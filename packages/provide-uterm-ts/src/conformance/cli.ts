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
 * `serve` is answered rather than implemented. This port has no conformance
 * server yet, and a stub one would make the matrix look complete while
 * proving nothing — the protocol's `--list-drivers` exists so an incomplete
 * matrix is visible instead.
 */

import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import { CLIENT_CAPABILITIES, type DriverResult, LANGUAGE, runClientScenario, type Scenario } from "./client-driver.ts";
import { errorMessage } from "./transport.ts";

/** What the command line is run with. Defaults are the real thing. */
export interface CliOptions {
  /** Where the one line goes. */
  write?: ((line: string) => void) | undefined;
  /** The fetch the driver's transport uses. */
  fetchImpl?: typeof fetch | undefined;
  /** How a scenario file is read. */
  readScenario?: ((path: string) => Promise<string>) | undefined;
}

/** How to run this driver. */
export const USAGE = "usage: <driver> client --base-url URL --token TOKEN --scenario FILE";

/** Why `serve` does nothing. */
export const SERVE_REFUSAL = "the TypeScript server role is not built: this port has no conformance server driver yet";

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
    write(JSON.stringify({ role: "server", language: LANGUAGE, status: "error", error: SERVE_REFUSAL }));
    return 1;
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

/**
 * Read `--name value` pairs.
 *
 * Flags nobody here knows are kept rather than refused, so a harness passing
 * one this driver has not learnt yet is not a run that never happened.
 */
function parseFlags(argv: string[]): Map<string, string> {
  const flags = new Map<string, string>();
  let name: string | null = null;
  for (const token of argv) {
    if (name === null) {
      if (!token.startsWith("--")) {
        throw new Error(`expected a --flag, got ${JSON.stringify(token)}; ${USAGE}`);
      }
      name = token.slice(2);
      continue;
    }
    flags.set(name, token);
    name = null;
  }
  if (name !== null) {
    throw new Error(`--${name} has no value; ${USAGE}`);
  }
  return flags;
}

/** A flag the client role cannot run without. */
function required(flags: Map<string, string>, name: string): string {
  const value = flags.get(name);
  if (value === undefined) {
    throw new Error(`--${name} is required; ${USAGE}`);
  }
  return value;
}
