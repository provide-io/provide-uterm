//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, sep } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { type DetectorSnapshot, FileScreenSaver } from "./index.ts";

interface SaverGolden {
  captured_at: number;
  base: DetectorSnapshot;
  headers: Array<{
    name: string;
    snapshot: DetectorSnapshot;
    prompt_id: string | null;
    filename: string;
    content: string;
  }>;
  behaviour: {
    first_filename: string;
    saved_twice: null;
    forced_filename: string;
    forced_again_filename: string;
    other_filename: string;
    count_after_three: number;
    disabled: null;
    count_before_clear: number;
    count_after_clear: number;
    saves_again_after_clear: boolean;
    namespaced_dir_tail: string[];
    shared_dir_tail: string[];
    namespaced_dir_tail_after_rename: string[];
    resaved_filename: string;
    empty_namespace_tail: string[];
    off_result: null;
    on_result_saved: boolean;
  };
  refused: Array<{ name: string; snapshot: DetectorSnapshot; saved: boolean }>;
  no_captured_at_saves: boolean;
}

const golden = loadGolden<SaverGolden>("saver_golden.json");

/** Directories made during a test, cleaned up afterwards. */
const made: string[] = [];

afterEach(() => {
  for (const directory of made.splice(0, made.length)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

/** A saver rooted somewhere temporary, timestamped the way the corpus was. */
function saver(options: ConstructorParameters<typeof FileScreenSaver>[1] = {}): FileScreenSaver {
  const base = mkdtempSync(join(tmpdir(), "uterm-saver-"));
  made.push(base);
  // The reference writes local time, so the corpus was recorded under a fixed
  // zone; the tests pin the same one rather than depending on where they run.
  return new FileScreenSaver(base, { timeZone: "UTC", ...options });
}

/** The path segments below a saver's base directory. */
function tail(subject: FileScreenSaver, base: string): string[] {
  return relative(base, subject.screensDir()).split(sep);
}

describe("what a saved screen file says", () => {
  it.each(golden.headers)("$name", (record) => {
    const subject = saver();
    const path = subject.saveScreen(record.snapshot, record.prompt_id ?? undefined);
    expect(path).toBeDefined();
    expect(basename(path as string)).toBe(record.filename);
    expect(readFileSync(path as string, "utf-8")).toBe(record.content);
  });

  it("names the file by time, hash and prompt", () => {
    // So a directory of captures sorts chronologically and a reader can find
    // the one they mean without opening any of them.
    const record = golden.headers.find((entry) => entry.name === "with a prompt id");
    expect(record?.filename).toMatch(/^\d{8}-\d{6}-[0-9a-f]{8}-command\.txt$/);
  });

  it("leaves the prompt out of the name when there was none", () => {
    expect(golden.headers[0]?.filename).toMatch(/^\d{8}-\d{6}-[0-9a-f]{8}\.txt$/);
  });

  it("leaves an empty prompt out of the name too", () => {
    // An empty id is no id; a trailing hyphen would suggest one was lost.
    const record = golden.headers.find((entry) => entry.name === "with an empty prompt id");
    expect(record?.filename).toBe(golden.headers[0]?.filename);
    expect(record?.content).not.toContain("Prompt ID:");
  });

  it("writes midnight as the start of the day, not the end of the last", () => {
    // A twelve-hour formatter reports it as hour 24, which would file the
    // capture under the wrong date.
    expect(golden.headers.find((entry) => entry.name === "captured at midnight")?.filename).toBe(
      "20251010-000000-abcdef01.txt",
    );
    expect(golden.headers.find((entry) => entry.name === "captured at midnight")?.content).toContain(
      "Timestamp: 2025-10-10 00:00:00",
    );
    expect(golden.headers.find((entry) => entry.name === "captured one second before midnight")?.filename).toBe(
      "20251009-235959-abcdef01.txt",
    );
  });

  it("takes only the first eight characters of the hash", () => {
    // Long enough to tell captures apart, short enough to read.
    expect(golden.headers[0]?.filename).toContain((golden.base.screen_hash as string).slice(0, 8));
    expect(golden.headers[0]?.filename).not.toContain(golden.base.screen_hash as string);
  });

  it("puts the screen after the header, separated by a rule", () => {
    const record = golden.headers.find((entry) => entry.name === "with a multi-line screen");
    expect(record?.content.endsWith("one\ntwo\nthree")).toBe(true);
    expect(record?.content).toContain(`${"=".repeat(80)}\n\n`);
  });

  it("records where the cursor was", () => {
    expect(golden.headers.find((entry) => entry.name === "with a cursor")?.content).toContain("Cursor: (12, 3)");
  });

  it("fills in a cursor coordinate that is missing", () => {
    // A partial cursor is still worth recording; refusing it would lose the
    // half that was known.
    expect(golden.headers.find((entry) => entry.name === "with a partial cursor")?.content).toContain(
      "Cursor: (12, 0)",
    );
    expect(golden.headers[0]?.content).toContain("Cursor: (0, 0)");
  });

  it("records the terminal size and type, with defaults", () => {
    // A capture without them is still readable; an 80x25 ANSI terminal is the
    // assumption a reader would make anyway.
    expect(golden.headers[0]?.content).toContain("Size: 80x25");
    expect(golden.headers[0]?.content).toContain("Terminal: ANSI");
    expect(golden.headers.find((entry) => entry.name === "with a size")?.content).toContain("Size: 132x43");
    expect(golden.headers.find((entry) => entry.name === "with a terminal type")?.content).toContain(
      "Terminal: xterm-256color",
    );
  });

  it("records what was detected, when anything was", () => {
    const record = golden.headers.find((entry) => entry.name === "with a detection");
    expect(record?.content).toContain("Prompt ID: command");
    expect(record?.content).toContain("Input Type: single_key");
    expect(record?.content).toContain("Idle: True");
  });

  it("fills in a detection that says nothing", () => {
    const record = golden.headers.find((entry) => entry.name === "with an empty detection");
    expect(record?.content).toContain("Input Type: unknown");
    expect(record?.content).toContain("Idle: False");
  });

  it("leaves the detection lines out when there was none", () => {
    expect(golden.headers[0]?.content).not.toContain("Input Type:");
    expect(golden.headers[0]?.content).not.toContain("Idle:");
  });

  it("records the cursor-at-end flag either way", () => {
    // False is as worth recording as true — it is why a prompt was not
    // matched, and an absent line would read as "not known".
    expect(golden.headers.find((entry) => entry.name === "with the cursor at the end")?.content).toContain(
      "Cursor at End: True",
    );
    expect(golden.headers.find((entry) => entry.name === "with the cursor not at the end")?.content).toContain(
      "Cursor at End: False",
    );
    expect(golden.headers[0]?.content).not.toContain("Cursor at End:");
  });

  it("records how long the screen had been still, to two places", () => {
    expect(golden.headers.find((entry) => entry.name === "with a time since the last change")?.content).toContain(
      "Time Since Last Change: 1.50s",
    );
    expect(golden.headers.find((entry) => entry.name === "with a time that needs rounding")?.content).toContain(
      "Time Since Last Change: 1.23s",
    );
  });

  it("records a zero rather than leaving it out", () => {
    // "It had just changed" is a fact; an absent line would mean "unknown".
    expect(golden.headers.find((entry) => entry.name === "with a zero time since the last change")?.content).toContain(
      "Time Since Last Change: 0.00s",
    );
  });

  it("writes the timestamp in the zone it was told", () => {
    // The reference writes local time. Pinning the zone is what makes a
    // capture comparable between machines.
    expect(golden.headers[0]?.content).toContain("Timestamp: 2025-10-09 08:53:20");
  });
});

describe("saving a screen once", () => {
  it("does not save the same screen twice", () => {
    // A terminal redraws constantly. Without this a session fills a disk with
    // copies of one screen.
    const subject = saver();
    expect(subject.saveScreen(golden.base)).toBeDefined();
    expect(subject.saveScreen(golden.base)).toBeUndefined();
    expect(golden.behaviour.saved_twice).toBeNull();
  });

  it("saves a different screen", () => {
    const subject = saver();
    subject.saveScreen(golden.base);
    expect(subject.saveScreen({ ...golden.base, screen: "other", screen_hash: "9999" })).toBeDefined();
    expect(subject.savedCount()).toBe(2);
  });

  it("counts the screens it kept, not the times it was asked", () => {
    const subject = saver();
    subject.saveScreen(golden.base);
    subject.saveScreen(golden.base);
    subject.saveScreen(golden.base, undefined, true);
    subject.saveScreen({ ...golden.base, screen: "other", screen_hash: "9999" });
    expect(subject.savedCount()).toBe(golden.behaviour.count_after_three);
  });

  it("forgets what it saved when told to", () => {
    const subject = saver();
    subject.saveScreen(golden.base);
    expect(subject.savedCount()).toBe(golden.behaviour.count_before_clear);
    subject.clearSavedHashes();
    expect(subject.savedCount()).toBe(golden.behaviour.count_after_clear);
    expect(subject.saveScreen(golden.base)).toBeDefined();
  });
});

describe("forcing a save", () => {
  it("keeps the earlier capture", () => {
    // The point of forcing is a second copy, not the destruction of the
    // first.
    const subject = saver();
    const first = subject.saveScreen(golden.base) as string;
    const forced = subject.saveScreen(golden.base, undefined, true) as string;
    expect(forced).not.toBe(first);
    expect(basename(forced)).toBe(golden.behaviour.forced_filename);
    expect(readFileSync(first, "utf-8")).toBeTruthy();
  });

  it("keeps counting up while names are taken", () => {
    const subject = saver();
    subject.saveScreen(golden.base);
    subject.saveScreen(golden.base, undefined, true);
    const again = subject.saveScreen(golden.base, undefined, true) as string;
    expect(basename(again)).toBe(golden.behaviour.forced_again_filename);
  });

  it("does not rename an unforced save that finds its own file", () => {
    // Reachable once the remembered hashes are cleared: the write is not
    // forced, the file is already there, and overwriting it is right —
    // forgetting a hash is asking for the capture to be taken again.
    const subject = saver();
    const first = subject.saveScreen(golden.base) as string;
    subject.clearSavedHashes();
    const again = subject.saveScreen(golden.base) as string;
    expect(basename(again)).toBe(golden.behaviour.resaved_filename);
    expect(again).toBe(first);
  });

  it("gives up rather than looping for ever", () => {
    // A real session never exhausts ten thousand names, so the limit is
    // lowered here — a guard that cannot be reached is a guard that cannot be
    // shown to work.
    const subject = saver({ maxDuplicateAttempts: 3 });
    subject.saveScreen(golden.base);
    subject.saveScreen(golden.base, undefined, true);
    subject.saveScreen(golden.base, undefined, true);
    expect(() => subject.saveScreen(golden.base, undefined, true)).toThrow(/Could not find free filename/);
  });

  it("does not rename when the name is free", () => {
    const subject = saver();
    expect(basename(subject.saveScreen(golden.base, undefined, true) as string)).toBe(golden.behaviour.first_filename);
  });
});

describe("refusing to save", () => {
  it.each(golden.refused)("$name", (record) => {
    expect(saver().saveScreen(record.snapshot)).toBeUndefined();
    expect(record.saved).toBe(false);
  });

  it("needs both a screen and a hash", () => {
    // Nothing to read back, or nothing to identify it by.
    expect(saver().saveScreen({ screen: "", screen_hash: "abc" })).toBeUndefined();
    expect(saver().saveScreen({ screen: "x", screen_hash: "" })).toBeUndefined();
  });

  it("saves nothing while it is switched off", () => {
    const subject = saver({ enabled: false });
    expect(subject.saveScreen(golden.base)).toBeUndefined();
    expect(golden.behaviour.disabled).toBeNull();
    subject.setEnabled(true);
    expect(subject.saveScreen(golden.base)).toBeDefined();
    expect(golden.behaviour.on_result_saved).toBe(true);
  });

  it("is on unless told otherwise", () => {
    expect(saver().enabled).toBe(true);
    expect(saver({ enabled: false }).enabled).toBe(false);
  });

  it("times a snapshot that carries no capture time from now", () => {
    // A producer that did not stamp its frame still gets a filed capture.
    const subject = saver();
    const path = subject.saveScreen({ screen: "x", screen_hash: "deadbeefcafe" });
    expect(path).toBeDefined();
    expect(golden.no_captured_at_saves).toBe(true);
    const stamped = saver({ now: () => golden.captured_at }).saveScreen({ screen: "x", screen_hash: "deadbeefcafe" });
    expect(basename(stamped as string)).toBe("20251009-085320-deadbeef.txt");
  });
});

describe("where screens are filed", () => {
  it("files a named session under its name", () => {
    // So one target's captures can be read back without the others.
    const base = mkdtempSync(join(tmpdir(), "uterm-saver-"));
    made.push(base);
    const subject = new FileScreenSaver(base, { namespace: "tw2002", timeZone: "UTC" });
    expect(tail(subject, base)).toStrictEqual(golden.behaviour.namespaced_dir_tail);
  });

  it("files an unnamed session in a shared place", () => {
    const base = mkdtempSync(join(tmpdir(), "uterm-saver-"));
    made.push(base);
    const subject = new FileScreenSaver(base, { timeZone: "UTC" });
    expect(tail(subject, base)).toStrictEqual(golden.behaviour.shared_dir_tail);
  });

  it("moves to a new place when renamed", () => {
    const base = mkdtempSync(join(tmpdir(), "uterm-saver-"));
    made.push(base);
    const subject = new FileScreenSaver(base, { timeZone: "UTC" });
    subject.setNamespace("other");
    expect(tail(subject, base)).toStrictEqual(golden.behaviour.namespaced_dir_tail_after_rename);
    expect(subject.namespace).toBe("other");
  });

  it("treats an empty name as no name", () => {
    const base = mkdtempSync(join(tmpdir(), "uterm-saver-"));
    made.push(base);
    const subject = new FileScreenSaver(base, { namespace: "", timeZone: "UTC" });
    expect(tail(subject, base)).toStrictEqual(golden.behaviour.empty_namespace_tail);
  });

  it("reads a detection block by whether the key is there", () => {
    // A snapshot carrying the key with nothing in it has said there was a
    // detection and it was empty, which is different from saying nothing.
    const subject = saver();
    const path = subject.saveScreen({ ...golden.base, prompt_detected: undefined }) as string;
    expect(readFileSync(path, "utf-8")).toContain("Input Type: unknown");
  });

  it("goes back to the shared place when the name is taken away", () => {
    const base = mkdtempSync(join(tmpdir(), "uterm-saver-"));
    made.push(base);
    const subject = new FileScreenSaver(base, { namespace: "tw2002", timeZone: "UTC" });
    subject.setNamespace(undefined);
    expect(tail(subject, base)).toStrictEqual(golden.behaviour.shared_dir_tail);
  });

  it("writes in the system zone when it is not told one", () => {
    // Which is what the reference does. Pinning a zone is the tests' doing,
    // not the saver's default.
    const base = mkdtempSync(join(tmpdir(), "uterm-saver-"));
    made.push(base);
    const subject = new FileScreenSaver(base);
    const path = subject.saveScreen(golden.base) as string;
    expect(basename(path)).toMatch(/^\d{8}-\d{6}-abcdef01\.txt$/);
    expect(readFileSync(path, "utf-8")).toMatch(/Timestamp: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/);
  });

  it("makes the directory it needs", () => {
    // Including its parents: nothing else creates them, and a capture lost to
    // a missing directory is a capture lost.
    const subject = saver({ namespace: "deep" });
    const path = subject.saveScreen(golden.base) as string;
    expect(dirname(path)).toBe(subject.screensDir());
    expect(readFileSync(path, "utf-8")).toBeTruthy();
  });
});
