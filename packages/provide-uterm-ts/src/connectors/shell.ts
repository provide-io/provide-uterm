//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The reference interactive connector.
 *
 * Port of the Python module `provide.uterm.server.connectors.shell`.
 *
 * The one connector with no network underneath it, which makes it the honest
 * place to pin the worker protocol itself: what a snapshot carries, what a
 * mode change emits, and what happens on input nobody planned for.
 */

import { createHash } from "node:crypto";
import { MAX_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION, PREFERRED_PROTOCOL_VERSION } from "../bridge/index.ts";
import type { SessionConnector, WorkerMessage } from "./base.ts";

/** Screen width the connector reports. */
export const SHELL_COLS = 80;

/** Screen height the connector reports. */
export const SHELL_ROWS = 25;

/** How many transcript entries are kept — the bound that fixes the screen size. */
export const SHELL_TRANSCRIPT_LIMIT = 10;

/** The only configuration key this connector understands. */
const VALID_CONFIG_KEYS = new Set(["input_mode"]);

/** How many characters of a nickname are kept. */
const NICKNAME_LIMIT = 24;

/** Options that exist so a test need not depend on the clock. */
export interface ShellConnectorOptions {
  /** Wall clock in seconds. */
  now?: () => number;
}

/** How CPython spells a boolean, which these strings are compared against. */
function pythonBool(value: boolean): string {
  return value ? "True" : "False";
}

/** One line of the transcript. */
interface Entry {
  speaker: string;
  text: string;
  ts: number;
}

/** The reference connector: a lightweight interactive session. */
export class ShellSessionConnector implements SessionConnector {
  readonly #sessionId: string;
  readonly #displayName: string;
  readonly #now: () => number;
  #connected = false;
  #inputMode: string;
  #paused = false;
  #turns = 0;
  #nickname = "user";
  #lastCommand: string | undefined;
  #banner = "";
  #transcript: Entry[] = [];

  constructor(
    sessionId: string,
    displayName: string,
    config: Record<string, unknown> = {},
    options: ShellConnectorOptions = {},
  ) {
    const settings = config;
    // Sorted, so the message is the same however the object was built. A
    // typo would otherwise be accepted and silently ignored, and the session
    // would run in a mode nobody asked for.
    const unknown = Object.keys(settings)
      .filter((key) => !VALID_CONFIG_KEYS.has(key))
      .sort();
    if (unknown.length > 0) {
      throw new Error(`unknown shell connector_config keys: [${unknown.map((key) => `'${key}'`).join(", ")}]`);
    }
    this.#sessionId = sessionId;
    this.#displayName = displayName;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#inputMode = String(settings.input_mode ?? "open");
    this.#resetState();
  }

  /** Start the upstream session. */
  async start(): Promise<void> {
    this.#connected = true;
  }

  /** Stop it. */
  async stop(): Promise<void> {
    this.#connected = false;
  }

  /** Whether it is live. */
  isConnected(): boolean {
    return this.#connected;
  }

  /** Nothing: this connector is driven entirely by input. */
  async pollMessages(): Promise<WorkerMessage[]> {
    return [];
  }

  /** Process one line of user input. */
  async handleInput(data: string): Promise<WorkerMessage[]> {
    const text = ShellSessionConnector.#normalizeInput(data);
    if (text === "") {
      this.#banner = "Empty input ignored.";
      return [this.#snapshot()];
    }
    this.#turns += 1;
    if (text.startsWith("/")) {
      const space = text.indexOf(" ");
      const command = space < 0 ? text : text.slice(0, space);
      const argument = space < 0 ? "" : text.slice(space + 1).trim();
      this.#lastCommand = command;
      return await this.#handleSlashCommand(command, argument);
    }
    this.#banner = "Input accepted.";
    this.#append("user", `${this.#nickname}: ${text}`);
    this.#append("session", `session: received "${text}"`);
    return [this.#snapshot()];
  }

  /** Process a control action. */
  async handleControl(action: string): Promise<WorkerMessage[]> {
    if (action === "pause") {
      this.#paused = true;
      this.#banner = "Exclusive control active. Input is still accepted.";
      this.#append("system", "control: hijack acquired");
    } else if (action === "resume") {
      this.#paused = false;
      this.#banner = "Exclusive control released.";
      this.#append("system", "control: released");
    } else if (action === "step") {
      this.#turns += 1;
      this.#banner = "Single-step acknowledged.";
      this.#append("system", `control: single step #${this.#turns}`);
    } else {
      // Ignored rather than refused: an action this connector does not know
      // is one another connector might, and the session should not die of it.
      this.#banner = `Ignored unknown control action: ${action}`;
      this.#append("system", `control: ignored ${action}`);
    }
    return [this.#snapshot()];
  }

  /** The current screen, as a snapshot message. */
  async getSnapshot(): Promise<WorkerMessage> {
    return this.#snapshot();
  }

  /** A human-readable description of the session's state. */
  async getAnalysis(): Promise<string> {
    return [
      `[interactive shell analysis — worker: ${this.#sessionId}]`,
      `input_mode: ${this.#inputMode}`,
      `paused: ${pythonBool(this.#paused)}`,
      `turn_counter: ${this.#turns}`,
      `transcript_entries: ${this.#transcript.length}`,
      `last_command: ${this.#lastCommand ?? "(none)"}`,
      `prompt_visible: ${pythonBool(this.#prompt().trim() !== "")}`,
    ].join("\n");
  }

  /**
   * Change the input mode.
   *
   * @throws {Error} On a mode that is not one — unlike an unknown control
   *   action, this is the caller naming something that does not exist.
   */
  async setMode(mode: string): Promise<WorkerMessage[]> {
    if (mode !== "open" && mode !== "hijack") {
      throw new Error(`invalid mode: ${mode}`);
    }
    this.#inputMode = mode;
    if (mode === "open") {
      // Otherwise a session left paused stays paused with nobody holding it.
      this.#paused = false;
    }
    this.#banner = `Input mode set to ${this.#modeLabel()}.`;
    this.#append("system", `mode: ${this.#modeLabel()}`);
    return [this.#hello(), this.#snapshot()];
  }

  /** Empty the transcript. */
  async clear(): Promise<WorkerMessage[]> {
    this.#transcript = [];
    this.#banner = "Transcript cleared.";
    return [this.#snapshot()];
  }

  /** Carriage returns and tabs are input too, just not their own lines. */
  static #normalizeInput(data: string): string {
    return data.replaceAll("\r", "\n").replaceAll("\t", " ").trim();
  }

  /** Put the session back the way it started. */
  #resetState(): void {
    this.#paused = false;
    this.#turns = 0;
    this.#nickname = "user";
    this.#lastCommand = undefined;
    this.#banner = "Ready. Type /help for commands.";
    this.#transcript = [];
    this.#append("system", "Session online.");
    this.#append("session", "Use /help, /mode open, /mode hijack, /clear, /status, /reset.");
  }

  /** Add a line, dropping the oldest once the bound is reached. */
  #append(speaker: string, text: string): void {
    this.#transcript.push({ speaker, text, ts: this.#now() });
    if (this.#transcript.length > SHELL_TRANSCRIPT_LIMIT) {
      this.#transcript.shift();
    }
  }

  /** How the mode reads on screen. */
  #modeLabel(): string {
    return this.#inputMode === "open" ? "Shared input" : "Exclusive hijack";
  }

  /** How the control state reads on screen. */
  #controlLabel(): string {
    return this.#paused ? "Paused for hijack" : "Live";
  }

  /** The prompt line. */
  #prompt(): string {
    return `${this.#nickname}> `;
  }

  /**
   * Draw the screen.
   *
   * The height clamp and the cursor clamps below can never fire as things
   * stand: eight fixed lines plus at most {@link SHELL_TRANSCRIPT_LIMIT}
   * transcript entries and two more come to twenty, and the last line is
   * always the prompt, which a nickname bound at {@link NICKNAME_LIMIT}
   * cannot push past the width. They are kept because the reference keeps
   * them — the bounds they guard are the ones that would change first.
   */
  #renderScreen(): string {
    const lines = [
      `\x1b[1;36m[${this.#displayName} (${this.#sessionId})]\x1b[0m`,
      "-".repeat(60),
      `\x1b[32mMode:\x1b[0m ${this.#modeLabel()}`,
      `\x1b[32mControl:\x1b[0m ${this.#controlLabel()}`,
      "\x1b[32mHelp:\x1b[0m /help /mode open|hijack /clear /nick /say /status /shell /reset",
      `\x1b[33m${this.#banner}\x1b[0m`,
      "",
      "\x1b[1mTranscript\x1b[0m",
      ...this.#transcript.map((entry) => `${entry.speaker.padStart(7)}: ${entry.text}`),
      "",
      this.#prompt(),
    ];
    // The last rows only: the screen is a fixed height, and the newest lines
    // are the ones worth keeping.
    return lines.slice(-SHELL_ROWS).join("\n");
  }

  /** The snapshot message for the current screen. */
  #snapshot(): WorkerMessage {
    const screen = this.#renderScreen();
    const rendered = screen.split("\n");
    // `split` always yields at least one element, so there is a last line.
    const lastLine = rendered.at(-1) as string;
    return {
      type: "snapshot",
      screen,
      // Clamped: a cursor past the last row or column is one the client
      // cannot draw.
      cursor: { x: Math.min(lastLine.length, SHELL_COLS - 1), y: Math.min(rendered.length - 1, SHELL_ROWS - 1) },
      cols: SHELL_COLS,
      rows: SHELL_ROWS,
      // The hash is what lets a client skip a redraw of an unchanged frame.
      screen_hash: createHash("sha256").update(screen, "utf8").digest("hex").slice(0, 16),
      cursor_at_end: true,
      has_trailing_space: false,
      prompt_detected: { prompt_id: "shell_prompt" },
      ts: this.#now(),
    };
  }

  /** The hello frame that tells the far end the mode and the protocol range. */
  #hello(): WorkerMessage {
    return {
      type: "worker_hello",
      input_mode: this.#inputMode,
      ts: this.#now(),
      protocol: {
        min: MIN_PROTOCOL_VERSION,
        max: MAX_PROTOCOL_VERSION,
        preferred: PREFERRED_PROTOCOL_VERSION,
      },
    };
  }

  /** Run a slash command, or say it is not one. */
  async #handleSlashCommand(command: string, argument: string): Promise<WorkerMessage[]> {
    const simple = this.#handleSimpleCommand(command);
    if (simple !== undefined) {
      return simple;
    }
    const withArgument = await this.#handleArgumentCommand(command, argument);
    if (withArgument !== undefined) {
      return withArgument;
    }
    this.#banner = `Unknown command: ${command}`;
    this.#append("system", `unknown command: ${command}`);
    return [this.#snapshot()];
  }

  /** The commands that take no argument. */
  #handleSimpleCommand(command: string): WorkerMessage[] | undefined {
    if (command === "/help") {
      this.#banner = "Command help printed below.";
      this.#append("system", "Commands: /help /clear /mode open|hijack /status /nick <name> /say <text> /shell /reset");
      return [this.#snapshot()];
    }
    if (command === "/clear") {
      this.#transcript = [];
      this.#banner = "Transcript cleared.";
      return [this.#snapshot()];
    }
    if (command === "/status") {
      this.#banner = "Session status printed below.";
      this.#append("system", `mode=${this.#inputMode} paused=${pythonBool(this.#paused)} turns=${this.#turns}`);
      return [this.#snapshot()];
    }
    if (command === "/shell") {
      this.#banner = "Shell response appended.";
      this.#append("session", "This hosted server is the reference implementation.");
      return [this.#snapshot()];
    }
    if (command === "/reset") {
      this.#resetState();
      this.#banner = "Session reset.";
      return [this.#hello(), this.#snapshot()];
    }
    return undefined;
  }

  /** The commands that take one. */
  async #handleArgumentCommand(command: string, argument: string): Promise<WorkerMessage[] | undefined> {
    if (command === "/mode") {
      const mode = argument.toLowerCase();
      if (mode !== "open" && mode !== "hijack") {
        this.#banner = "Usage: /mode open|hijack";
        this.#append("system", "usage: /mode open|hijack");
        return [this.#snapshot()];
      }
      return await this.setMode(mode);
    }
    if (command === "/nick") {
      if (argument === "") {
        this.#banner = "Usage: /nick <name>";
        this.#append("system", "usage: /nick <name>");
        return [this.#snapshot()];
      }
      this.#nickname = argument.slice(0, NICKNAME_LIMIT);
      this.#banner = `Nickname set to ${this.#nickname}.`;
      this.#append("system", `nickname: ${this.#nickname}`);
      return [this.#snapshot()];
    }
    if (command === "/say") {
      if (argument === "") {
        this.#banner = "Usage: /say <text>";
        this.#append("system", "usage: /say <text>");
        return [this.#snapshot()];
      }
      this.#banner = "Message appended.";
      this.#append("user", `${this.#nickname}: ${argument}`);
      return [this.#snapshot()];
    }
    return undefined;
  }
}
