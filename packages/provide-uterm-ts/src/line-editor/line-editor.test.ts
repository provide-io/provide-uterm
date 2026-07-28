//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { LineEditor } from "./index.ts";

interface LineEditorGolden {
  cases: Array<{
    name: string;
    max_length: number;
    password_mode: boolean;
    chars: string[];
    steps: Array<{ char: string; emitted: string; line: string | null; buffer: string; cursor: number }>;
  }>;
  silent_mode: { buffer: string; cursor: number };
}

const golden = loadGolden<LineEditorGolden>("line_editor_golden.json");

/** An editor plus a capture of everything it wrote. */
function makeEditor(options: { maxLength?: number; passwordMode?: boolean } = {}): {
  editor: LineEditor;
  writes: string[];
} {
  const writes: string[] = [];
  const editor = new LineEditor({
    ...options,
    onWrite: (data: string) => {
      writes.push(data);
      return Promise.resolve();
    },
  });
  return { editor, writes };
}

/** Feed `chars` and return what was written for the final character. */
async function feed(editor: LineEditor, writes: string[], chars: string): Promise<string> {
  let emitted = "";
  for (const char of chars) {
    writes.length = 0;
    await editor.processChar(char);
    emitted = writes.join("");
  }
  return emitted;
}

describe("LineEditor line completion", () => {
  it("returns the buffer and clears state on carriage return", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc");
    writes.length = 0;
    expect(await editor.processChar("\r")).toBe("abc");
    expect(writes.join("")).toBe("\r\n");
    expect(editor.buffer).toBe("");
    expect(editor.cursorPos).toBe(0);
  });

  it("treats a newline the same as a carriage return", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "ab");
    expect(await editor.processChar("\n")).toBe("ab");
  });

  it("returns an empty string for an empty line", async () => {
    const { editor } = makeEditor();
    expect(await editor.processChar("\r")).toBe("");
  });

  it("returns null for every character that is not a line terminator", async () => {
    const { editor } = makeEditor();
    expect(await editor.processChar("a")).toBeNull();
  });
});

describe("LineEditor editing", () => {
  it("echoes an inserted character at the end of the line", async () => {
    const { editor, writes } = makeEditor();
    expect(await feed(editor, writes, "a")).toBe("a");
  });

  it("redraws the tail when inserting mid-line", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc\x01");
    writes.length = 0;
    await editor.processChar("X");
    expect(writes.join("")).toBe("Xabc\x1b[3D");
    expect(editor.buffer).toBe("Xabc");
    expect(editor.cursorPos).toBe(1);
  });

  it("removes the character before the cursor on backspace", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc\x7f");
    expect(editor.buffer).toBe("ab");
    expect(editor.cursorPos).toBe(2);
  });

  it("accepts the alternate backspace code", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc\x08");
    expect(editor.buffer).toBe("ab");
  });

  it("ignores backspace at the start of the line", async () => {
    const { editor, writes } = makeEditor();
    expect(await feed(editor, writes, "\x7f")).toBe("");
    expect(editor.buffer).toBe("");
  });

  it("rings the bell instead of inserting past the length limit", async () => {
    const { editor, writes } = makeEditor({ maxLength: 3 });
    expect(await feed(editor, writes, "abcd")).toBe("\x07");
    expect(editor.buffer).toBe("abc");
  });
});

describe("LineEditor cursor movement", () => {
  it("moves to the start of the line on ctrl-a", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc");
    writes.length = 0;
    await editor.processChar("\x01");
    expect(writes.join("")).toBe("\x1b[3D");
    expect(editor.cursorPos).toBe(0);
  });

  it("moves to the end of the line on ctrl-e", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc\x01");
    writes.length = 0;
    await editor.processChar("\x05");
    expect(writes.join("")).toBe("\x1b[3C");
    expect(editor.cursorPos).toBe(3);
  });

  it("moves one character left on ctrl-b and right on ctrl-f", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc");
    writes.length = 0;
    await editor.processChar("\x02");
    expect(writes.join("")).toBe("\x1b[D");
    expect(editor.cursorPos).toBe(2);
    writes.length = 0;
    await editor.processChar("\x06");
    expect(writes.join("")).toBe("\x1b[C");
    expect(editor.cursorPos).toBe(3);
  });

  it("writes nothing when a movement would leave the line", async () => {
    const { editor, writes } = makeEditor();
    expect(await feed(editor, writes, "\x01")).toBe("");
    expect(await feed(editor, writes, "\x02")).toBe("");
    expect(await feed(editor, writes, "\x05")).toBe("");
    expect(await feed(editor, writes, "\x06")).toBe("");
  });
});

describe("LineEditor kill operations", () => {
  it("kills backward to the start of the line on ctrl-u", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc");
    writes.length = 0;
    await editor.processChar("\x15");
    expect(writes.join("")).toBe("\x1b[3D\x1b[K");
    expect(editor.buffer).toBe("");
    expect(editor.cursorPos).toBe(0);
  });

  it("keeps and redraws the tail when ctrl-u runs mid-line", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abcd\x02\x02");
    writes.length = 0;
    await editor.processChar("\x15");
    expect(writes.join("")).toBe("\x1b[2Dcd\x1b[K\x1b[2D");
    expect(editor.buffer).toBe("cd");
    expect(editor.cursorPos).toBe(0);
  });

  it("kills forward to the end of the line on ctrl-k", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abcd\x02\x02");
    writes.length = 0;
    await editor.processChar("\x0b");
    expect(writes.join("")).toBe("\x1b[K");
    expect(editor.buffer).toBe("ab");
  });

  it("kills the previous word on ctrl-w", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "hello");
    writes.length = 0;
    await editor.processChar("\x17");
    expect(writes.join("")).toBe("\x1b[5D\x1b[K");
    expect(editor.buffer).toBe("");
  });

  it("skips trailing spaces before killing a word", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "one two   ");
    writes.length = 0;
    await editor.processChar("\x17");
    expect(writes.join("")).toBe("\x1b[6D\x1b[K");
    expect(editor.buffer).toBe("one ");
  });

  it("kills a run of spaces when there is no word before it", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "   ");
    writes.length = 0;
    await editor.processChar("\x17");
    expect(editor.buffer).toBe("");
  });
});

describe("LineEditor password mode", () => {
  it("echoes an asterisk instead of the character", async () => {
    const { editor, writes } = makeEditor({ passwordMode: true });
    expect(await feed(editor, writes, "s")).toBe("*");
  });

  it("keeps the real characters in the buffer", async () => {
    const { editor, writes } = makeEditor({ passwordMode: true });
    await feed(editor, writes, "secret");
    expect(editor.buffer).toBe("secret");
  });

  it("masks the redrawn tail as well", async () => {
    const { editor, writes } = makeEditor({ passwordMode: true });
    await feed(editor, writes, "abc\x01");
    writes.length = 0;
    await editor.processChar("X");
    expect(writes.join("")).toBe("****\x1b[3D");
  });
});

describe("LineEditor configuration", () => {
  it("tracks state with no write callback configured", async () => {
    const editor = new LineEditor();
    for (const char of "abc\x7f") {
      await editor.processChar(char);
    }
    expect({ buffer: editor.buffer, cursor: editor.cursorPos }).toStrictEqual(golden.silent_mode);
  });

  it("clears the buffer and cursor on reset", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc");
    editor.reset();
    expect({ buffer: editor.buffer, cursor: editor.cursorPos }).toStrictEqual({ buffer: "", cursor: 0 });
  });

  it("exposes the buffer through getBuffer", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "abc");
    expect(editor.getBuffer()).toBe("abc");
  });

  it("applies a new maximum length immediately", async () => {
    const { editor, writes } = makeEditor({ maxLength: 80 });
    editor.setMaxLength(2);
    expect(await feed(editor, writes, "abc")).toBe("\x07");
    expect(editor.buffer).toBe("ab");
  });

  it("applies a password-mode change immediately", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "a");
    editor.setPasswordMode(true);
    expect(await feed(editor, writes, "b")).toBe("*");
    editor.setPasswordMode(false);
    expect(await feed(editor, writes, "c")).toBe("c");
  });

  it("defaults to an eighty-character line", async () => {
    const { editor, writes } = makeEditor();
    await feed(editor, writes, "a".repeat(80));
    expect(await feed(editor, writes, "a")).toBe("\x07");
  });
});

describe("differential parity with CPython", () => {
  it("matches every recorded step of every case", async () => {
    for (const testCase of golden.cases) {
      const { editor, writes } = makeEditor({
        maxLength: testCase.max_length,
        passwordMode: testCase.password_mode,
      });
      const actual: Array<Record<string, unknown>> = [];
      for (const char of testCase.chars) {
        writes.length = 0;
        const line = await editor.processChar(char);
        actual.push({
          char,
          emitted: writes.join(""),
          line,
          buffer: editor.buffer,
          cursor: editor.cursorPos,
        });
      }
      expect({ name: testCase.name, steps: actual }).toStrictEqual({
        name: testCase.name,
        steps: testCase.steps,
      });
    }
    expect(golden.cases.length).toBeGreaterThan(30);
  });
});
