//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { CommandDispatcher, heading, PROMPT } from "./index.ts";

interface DispatcherGolden {
  prompt: string;
  help: string;
  command_help: Record<string, string>;
  lines: Array<{ name: string; line: string; output: string[] }>;
  env: Array<{ name: string; keys: string[]; output: string[] }>;
}

const golden = loadGolden<DispatcherGolden>("dispatcher_golden.json");

/** A dispatcher carrying the same help the reference had. */
function dispatcher(options: { context?: Record<string, string>; commands?: Record<string, () => string[]> } = {}) {
  return new CommandDispatcher({
    help: golden.help,
    commandHelp: golden.command_help,
    ...(options.context === undefined ? {} : { context: options.context }),
    ...(options.commands === undefined ? {} : { commands: options.commands }),
  });
}

describe("where a line goes", () => {
  it.each(golden.lines)("$name", async (record) => {
    expect(await dispatcher().dispatch(record.line)).toEqual(record.output);
  });

  it("answers an empty line with a prompt, not an error", async () => {
    // Somebody pressing return has not made a mistake.
    for (const line of ["", "   ", "\t", "\n"]) {
      expect(await dispatcher().dispatch(line)).toEqual([PROMPT]);
    }
  });

  it("answers an interrupt with a prompt", async () => {
    // The terminal has already echoed it; saying anything else would be
    // shouting at somebody who pressed Ctrl-C.
    expect(await dispatcher().dispatch("\x03")).toEqual([PROMPT]);
  });

  it("takes the command however it was capitalised", async () => {
    for (const line of ["HELP", "HeLp", "help"]) {
      expect((await dispatcher().dispatch(line))[0]).toContain(golden.help);
    }
  });

  it("hands a command everything after its name, as one argument", async () => {
    // A command taking a sentence gets the sentence, not its first word.
    let seen: string | undefined;
    const dispatch = dispatcher({
      commands: {
        say: ((argument: string) => {
          seen = argument;
          return [PROMPT];
        }) as unknown as () => string[],
      },
    });
    await dispatch.dispatch("say  hello there,  world  ");
    expect(seen).toBe("hello there,  world");
  });

  it("hands a command nothing when there was nothing after its name", async () => {
    let seen: string | undefined;
    const dispatch = dispatcher({
      commands: {
        say: ((argument: string) => {
          seen = argument;
          return [PROMPT];
        }) as unknown as () => string[],
      },
    });
    await dispatch.dispatch("say");
    expect(seen).toBe("");
  });

  it("says so when a line names nothing, and says where to look", async () => {
    // Quietly doing nothing is the worst of the three possible answers.
    const output = (await dispatcher().dispatch("sideways"))[0] as string;
    expect(output).toContain("unknown command");
    expect(output).toContain("sideways");
    expect(output).toContain("help");
  });

  it("ignores what follows a command that takes nothing", async () => {
    expect(await dispatcher().dispatch("clear all")).toEqual(await dispatcher().dispatch("clear"));
    expect(await dispatcher().dispatch("exit now")).toEqual(await dispatcher().dispatch("exit"));
  });

  it("ends a session on any of the three ways of asking", async () => {
    const goodbye = await dispatcher().dispatch("exit");
    for (const line of ["quit", "\x04", "EXIT"]) {
      expect(await dispatcher().dispatch(line)).toEqual(goodbye);
    }
  });

  it("ends every answer with a prompt", async () => {
    // Otherwise the next thing somebody types has nothing to type at.
    for (const line of ["", "help", "clear", "exit", "sideways", "env", "help render"]) {
      const output = await dispatcher().dispatch(line);
      expect((output.at(-1) as string).endsWith(PROMPT)).toBe(true);
    }
  });
});

describe("a dispatcher built with nothing", () => {
  it("still answers every built-in", async () => {
    // A host that supplies no commands and no help still has a usable shell.
    const bare = new CommandDispatcher();
    expect(await bare.dispatch("")).toEqual([PROMPT]);
    expect(await bare.dispatch("clear")).toEqual([`\x1b[2J\x1b[H${PROMPT}`]);
    expect((await bare.dispatch("exit"))[0]).toContain("Goodbye");
    expect(await bare.dispatch("help")).toEqual([PROMPT]);
    expect((await bare.dispatch("help anything"))[0]).toContain("no help for");
    expect((await bare.dispatch("env"))[0]).toContain("(empty context)");
    expect((await bare.dispatch("anything"))[0]).toContain("unknown command");
  });

  it("quotes a command name the way the reference does", async () => {
    // So a message read in a transcript is the same message.
    const bare = new CommandDispatcher();
    expect((await bare.dispatch("plain"))[0]).toContain("'plain'");
    // A name holding an apostrophe is quoted the other way round rather than
    // escaped, which is what Python's `repr` does.
    expect((await bare.dispatch("it's"))[0]).toContain(`"it's"`);
    // Holding both — in one word, since only the first word is the command —
    // it is single-quoted with the apostrophe escaped.
    expect((await bare.dispatch(`it's"x`))[0]).toContain(`'it\\'s"x'`);
  });

  it("shows a name with no description as just the name", async () => {
    const output = (await new CommandDispatcher({ context: { alone: "" } }).dispatch("env"))[0] as string;
    expect(output).toContain("alone");
  });
});

describe("asking for help", () => {
  it("lists everything when asked with no argument", async () => {
    expect((await dispatcher().dispatch("help"))[0]).toBe(`${golden.help}${PROMPT}`);
  });

  it("describes one command when named", async () => {
    const name = Object.keys(golden.command_help)[0] as string;
    expect((await dispatcher().dispatch(`help ${name}`))[0]).toBe(`${golden.command_help[name]}${PROMPT}`);
  });

  it("takes the name however it was capitalised", async () => {
    const name = Object.keys(golden.command_help)[0] as string;
    expect(await dispatcher().dispatch(`help ${name.toUpperCase()}`)).toEqual(
      await dispatcher().dispatch(`help ${name}`),
    );
  });

  it("says there is none rather than showing an empty page", async () => {
    const output = (await dispatcher().dispatch("help sideways"))[0] as string;
    expect(output).toContain("no help for");
    expect(output).toContain("sideways");
  });

  it("has help for every command it was given", async () => {
    for (const name of Object.keys(golden.command_help)) {
      expect((await dispatcher().dispatch(`help ${name}`))[0]).not.toContain("no help for");
    }
  });
});

describe("what the shell was handed", () => {
  it.each(golden.env)("$name", async (record) => {
    // The reference reads the names off whatever object it was given; here
    // they are passed in, so the corpus's key list is what is reproduced.
    const context = Object.fromEntries(record.keys.map((key) => [key, ""]));
    const output = (await dispatcher({ context }).dispatch("env"))[0] as string;
    const expected = record.output[0] as string;
    if (record.keys.filter((key) => !key.startsWith("_")).length === 0) {
      expect(output).toBe(expected);
      return;
    }
    for (const key of record.keys.filter((key) => !key.startsWith("_"))) {
      expect(output).toContain(key);
    }
    // Under the same heading the reference writes, so a listing is not a bare
    // column of names somebody has to guess the meaning of.
    expect(output.startsWith(heading("context"))).toBe(true);
    expect(expected.startsWith(heading("context"))).toBe(true);
  });

  it("says the context is empty rather than showing a heading over nothing", async () => {
    // An empty heading reads as something having gone wrong.
    const output = (await dispatcher({ context: {} }).dispatch("env"))[0] as string;
    expect(output).toContain("(empty context)");
    expect(output).not.toContain("context\r\n");
  });

  it("leaves out the names that are not somebody's to see", async () => {
    const output = (await dispatcher({ context: { _hidden: "", shown: "" } }).dispatch("env"))[0] as string;
    expect(output).toContain("shown");
    expect(output).not.toContain("_hidden");
  });

  it("says the context is empty when everything in it is private", async () => {
    const output = (await dispatcher({ context: { _a: "", _b: "" } }).dispatch("env"))[0] as string;
    expect(output).toContain("(empty context)");
  });

  it("lists the names in order, whatever order they were given in", async () => {
    const output = (await dispatcher({ context: { zebra: "", apple: "", mango: "" } }).dispatch("env"))[0] as string;
    expect(output.indexOf("apple")).toBeLessThan(output.indexOf("mango"));
    expect(output.indexOf("mango")).toBeLessThan(output.indexOf("zebra"));
  });

  it("shows what each name is, when the host said", async () => {
    const output = (await dispatcher({ context: { SESSIONS: "DurableObject" } }).dispatch("env"))[0] as string;
    expect(output).toContain("SESSIONS");
    expect(output).toContain("DurableObject");
  });
});

describe("the commands a host supplies", () => {
  it("routes to one that was given", async () => {
    const dispatch = dispatcher({ commands: { fetch: () => ["fetched", PROMPT] } });
    expect(await dispatch.dispatch("fetch https://example.test")).toEqual(["fetched", PROMPT]);
  });

  it("refuses one that was not, rather than pretending", async () => {
    // This port does not carry the reference's Python sandbox, so `py` is a
    // command a host either supplies or does not have. Silently accepting it
    // would be worse than saying so.
    const output = (await dispatcher().dispatch("py 1 + 1"))[0] as string;
    expect(output).toContain("unknown command");
    expect(output).toContain("py");
  });

  it("waits for a command that takes its time", async () => {
    const dispatch = dispatcher({
      commands: {
        slow: (async () => {
          await new Promise((resolve) => setTimeout(resolve, 1));
          return ["done", PROMPT];
        }) as unknown as () => string[],
      },
    });
    expect(await dispatch.dispatch("slow")).toEqual(["done", PROMPT]);
  });

  it("cannot be made to answer for a built-in", async () => {
    // A host cannot replace `help`, `clear`, `env` or leaving: they are what
    // somebody falls back on when a supplied command misbehaves.
    const dispatch = dispatcher({
      commands: {
        help: () => ["hijacked", PROMPT],
        clear: () => ["hijacked", PROMPT],
        env: () => ["hijacked", PROMPT],
        exit: () => ["hijacked", PROMPT],
      },
    });
    for (const line of ["help", "clear", "env", "exit"]) {
      expect(await dispatch.dispatch(line)).not.toEqual(["hijacked", PROMPT]);
    }
  });

  it("matches a supplied command by its lowercased name", async () => {
    const dispatch = dispatcher({ commands: { fetch: () => ["fetched", PROMPT] } });
    expect(await dispatch.dispatch("FETCH x")).toEqual(["fetched", PROMPT]);
  });
});
