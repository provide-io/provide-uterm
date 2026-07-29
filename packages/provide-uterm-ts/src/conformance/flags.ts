//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The driver's `--name value` arguments.
 *
 * Its own module because both roles read them and neither owns the other: the
 * client parses `--scenario`, the server parses `--auth`, and the rules for
 * what an argument list *is* are the same for both.
 */

/** How to run this driver. */
export const USAGE =
  "usage: <driver> serve [--auth MODE] | <driver> client --base-url URL --token TOKEN --scenario FILE";

/**
 * Read `--name value` pairs.
 *
 * Flags nobody here knows are kept rather than refused, so a harness passing
 * one this driver has not learnt yet is not a run that never happened.
 *
 * @throws {Error} When a token is not a flag, or a flag has no value.
 */
export function parseFlags(argv: readonly string[]): Map<string, string> {
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

/** A flag a role cannot run without. */
export function required(flags: ReadonlyMap<string, string>, name: string): string {
  const value = flags.get(name);
  if (value === undefined) {
    throw new Error(`--${name} is required; ${USAGE}`);
  }
  return value;
}
