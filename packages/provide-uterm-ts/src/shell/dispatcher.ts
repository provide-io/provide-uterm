//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What a line typed at the shell becomes.
 *
 * Port of `provide.uterm.shell.commands.dispatcher` — the routing, which is
 * what is the same on every runtime.
 *
 * * **A command is the first word, lowercased**, and everything after it is
 *   one argument rather than a list, so a command taking a sentence gets the
 *   sentence.
 * * **An empty line is a prompt, not an error**, and so is a bare interrupt,
 *   which the terminal has already echoed.
 * * **A line that names nothing says so**, and says where to look. An
 *   unrecognised command quietly doing nothing is the worst of the three
 *   possible answers.
 *
 * The commands that reach out — a fetch, a key-value store, a Durable Object
 * — are supplied by whoever builds the dispatcher rather than built in, so
 * this decides *where a line goes* and nothing about what is at the far end.
 */

import { BOLD, CLEAR_SCREEN, errorMsg, fmtKv, heading, infoMsg, PROMPT, RESET } from "./output.ts";

/** A command's output: the lines to write, ending with a prompt. */
export type CommandOutput = string[];

/** A command supplied by whoever is hosting the shell. */
export type ShellCommand = (argument: string) => Promise<CommandOutput> | CommandOutput;

/** What the dispatcher is built with. */
export interface DispatcherOptions {
  /** The commands that reach outside: `py`, `kv`, `fetch`, `storage`, and so on. */
  commands?: Readonly<Record<string, ShellCommand>>;
  /** The help text shown by a bare `help`. */
  help?: string;
  /** Help for one command, keyed by its name. */
  commandHelp?: Readonly<Record<string, string>>;
  /** What `env` lists: the names the host wants shown, and what each one is. */
  context?: Readonly<Record<string, string>>;
}

/** The lines that end a session. */
const GOODBYE = new Set(["exit", "quit", "\x04"]);

/** Route a line to a command. */
export class CommandDispatcher {
  readonly #commands: Readonly<Record<string, ShellCommand>>;
  readonly #help: string;
  readonly #commandHelp: Readonly<Record<string, string>>;
  readonly #context: Readonly<Record<string, string>>;

  constructor(options: DispatcherOptions = {}) {
    this.#commands = options.commands ?? {};
    this.#help = options.help ?? "";
    this.#commandHelp = options.commandHelp ?? {};
    this.#context = options.context ?? {};
  }

  /** Handle one completed line. */
  async dispatch(line: string): Promise<CommandOutput> {
    const trimmed = line.trim();
    if (trimmed === "" || trimmed === "\x03") {
      // Nothing typed, or an interrupt the terminal has already echoed.
      return [PROMPT];
    }

    // Split once: everything after the first word is a single argument, so a
    // command taking a sentence is handed the sentence.
    const separator = trimmed.search(/\s/);
    const command = (separator === -1 ? trimmed : trimmed.slice(0, separator)).toLowerCase();
    const argument = separator === -1 ? "" : trimmed.slice(separator).trim();

    if (GOODBYE.has(command)) {
      return [`${infoMsg("Goodbye.\r\n")}${PROMPT}`];
    }
    if (command === "help") {
      return [this.#helpFor(argument)];
    }
    if (command === "clear") {
      return [`${CLEAR_SCREEN}${PROMPT}`];
    }
    if (command === "env") {
      return [this.#env()];
    }

    const handler = this.#commands[command];
    if (handler !== undefined) {
      return handler(argument);
    }
    return [`${errorMsg(`unknown command: ${quote(command)} — type ${BOLD}help${RESET}`)}${PROMPT}`];
  }

  /** The help for one command, or the whole list. */
  #helpFor(argument: string): string {
    if (argument === "") {
      return `${this.#help}${PROMPT}`;
    }
    const detail = this.#commandHelp[argument.toLowerCase()];
    if (detail === undefined) {
      return `${errorMsg(`no help for ${quote(argument)}`)}${PROMPT}`;
    }
    return `${detail}${PROMPT}`;
  }

  /** What the shell was handed to work with. */
  #env(): string {
    // Entries rather than names, so each description comes from the same read
    // as the name it belongs to.
    const shown = Object.entries(this.#context)
      .filter(([name]) => !name.startsWith("_"))
      // Two-way: the names are an object's keys, so no two are equal. Sorted
      // by code unit, as the reference's `sorted` does.
      .sort(([left], [right]) => (left < right ? -1 : 1));
    if (shown.length === 0) {
      // An empty heading reads as something having gone wrong.
      return `${infoMsg("(empty context)")}${PROMPT}`;
    }
    return `${heading("context")}${shown.map(([name, value]) => fmtKv(name, value)).join("")}${PROMPT}`;
  }
}

/** A value as Python's `repr` quotes it, which is how the messages read. */
function quote(value: string): string {
  return value.includes("'") && !value.includes('"') ? `"${value}"` : `'${value.replaceAll("'", "\\'")}'`;
}
