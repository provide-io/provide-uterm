//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Persisting unique screens to disk.
 *
 * Port of the Python module `provide.uterm.detection.saver`.
 *
 * A saved screen is what somebody reads back weeks later to work out what a
 * session was doing, so the file is a record and the header is most of its
 * value. The hash is the screen's identity: a terminal redraws constantly,
 * and without that check a session fills a disk with copies of one screen.
 *
 * **Blocking.** These are synchronous disk writes, called from the engine's
 * asynchronous path. At a few saves a second that is fine; at higher rates it
 * is worth moving off the main thread.
 */

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { DetectorSnapshot } from "./detector.ts";

/** Options for {@link FileScreenSaver}. */
export interface ScreenSaverOptions {
  namespace?: string;
  enabled?: boolean;
  /** The zone timestamps are written in. Defaults to the system's. */
  timeZone?: string;
  /** Wall clock in seconds, for a snapshot that carries no capture time. */
  now?: () => number;
  /**
   * How many names a forced save will try before giving up.
   *
   * Only ever lowered by a test: the default is far past anything a session
   * produces, and a limit that cannot be reached is a limit that cannot be
   * shown to work.
   */
  maxDuplicateAttempts?: number;
}

/** How many names a forced save will try before giving up. */
const MAX_DUPLICATE_ATTEMPTS = 10_000;

/** The rule that separates the header from the screen. */
const RULE = "=".repeat(80);

/** Two digits, for a clock component. */
function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

/** The parts of an instant, in the zone a saver was told to use. */
function parts(epochSeconds: number, timeZone: string | undefined): Record<string, number> {
  const formatter = new Intl.DateTimeFormat("en-US", {
    ...(timeZone === undefined ? {} : { timeZone }),
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    // `h23` rather than `hour12: false`: the latter has historically mapped
    // to `h24` in some implementations, which writes midnight as hour 24 and
    // files the capture under the previous day.
    hourCycle: "h23",
  });
  const found: Record<string, number> = {};
  for (const part of formatter.formatToParts(new Date(epochSeconds * 1000))) {
    if (part.type !== "literal") {
      found[part.type] = Number(part.value);
    }
  }
  return found;
}

/** A Python `bool`, as its `str()` renders it. */
function pyBool(value: unknown): string {
  return value === true ? "True" : "False";
}

/** Writes screens to a directory, one file each. */
export class FileScreenSaver {
  readonly #baseDir: string;
  readonly #timeZone: string | undefined;
  readonly #now: () => number;
  readonly #maxDuplicateAttempts: number;
  readonly #savedHashes = new Set<string>();
  #namespace: string | undefined;
  #enabled: boolean;

  constructor(baseDir: string, options: ScreenSaverOptions = {}) {
    this.#baseDir = baseDir;
    this.#namespace = options.namespace;
    this.#enabled = options.enabled ?? true;
    this.#timeZone = options.timeZone;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#maxDuplicateAttempts = options.maxDuplicateAttempts ?? MAX_DUPLICATE_ATTEMPTS;
  }

  /** Whether anything is being written. */
  get enabled(): boolean {
    return this.#enabled;
  }

  /** What this session is called, if anything. */
  get namespace(): string | undefined {
    return this.#namespace;
  }

  /** Turn writing on or off. */
  setEnabled(enabled: boolean): void {
    this.#enabled = enabled;
  }

  /** Rename the session, which moves where its screens are filed. */
  setNamespace(namespace: string | undefined): void {
    this.#namespace = namespace;
  }

  /**
   * Where screens are filed.
   *
   * A named session gets its own place, so one target's captures can be read
   * back without the others; an unnamed one goes somewhere shared.
   */
  screensDir(): string {
    if (this.#namespace !== undefined && this.#namespace !== "") {
      return join(this.#baseDir, "games", this.#namespace, "screens");
    }
    return join(this.#baseDir, "shared", "screens");
  }

  /**
   * The name to write under, avoiding an existing file when forced.
   *
   * @throws {Error} When no free name is found.
   */
  #resolveFilePath(screensDir: string, filename: string, force: boolean): string {
    const path = join(screensDir, filename);
    if (!force || !existsSync(path)) {
      return path;
    }
    // A distinct name rather than an overwrite: the point of forcing is a
    // second copy, not the destruction of the first.
    const stem = filename.replace(/\.txt$/, "");
    for (let attempt = 1; attempt < this.#maxDuplicateAttempts; attempt += 1) {
      const candidate = join(screensDir, `${stem}-dup${attempt}.txt`);
      if (!existsSync(candidate)) {
        return candidate;
      }
    }
    throw new Error(`Could not find free filename after 10,000 attempts for ${filename}`);
  }

  /**
   * Write a screen, unless it has been written before.
   *
   * @returns Where it was written, or nothing when it was not.
   */
  saveScreen(snapshot: DetectorSnapshot, promptId?: string, force = false): string | undefined {
    if (!this.#enabled) {
      return undefined;
    }

    const screen = (snapshot.screen ?? "") as string;
    const screenHash = (snapshot.screen_hash ?? "") as string;
    const capturedAt = typeof snapshot.captured_at === "number" ? snapshot.captured_at : this.#now();

    if (screen === "" || screenHash === "") {
      // Nothing to read back, or nothing to identify it by.
      return undefined;
    }
    if (!force && this.#savedHashes.has(screenHash)) {
      return undefined;
    }

    const screensDir = this.screensDir();
    // Parents included: nothing else makes them, and a capture lost to a
    // missing directory is a capture lost.
    mkdirSync(screensDir, { recursive: true });

    const when = parts(capturedAt, this.#timeZone);
    const timestamp = `${when.year}${pad(when.month as number)}${pad(when.day as number)}-${pad(when.hour as number)}${pad(when.minute as number)}${pad(when.second as number)}`;
    // Eight characters: long enough to tell captures apart, short enough to
    // read in a directory listing.
    const filename = `${timestamp}-${screenHash.slice(0, 8)}${promptId === undefined || promptId === "" ? "" : `-${promptId}`}.txt`;

    const path = this.#resolveFilePath(screensDir, filename, force);
    writeFileSync(path, this.#formatScreenFile(snapshot, screen, promptId, capturedAt), "utf-8");

    // Remembered only after the write succeeded, so a failed save is retried
    // rather than silently skipped for ever.
    this.#savedHashes.add(screenHash);
    return path;
  }

  /** The file's contents: a header a reader can place, then the screen. */
  #formatScreenFile(
    snapshot: DetectorSnapshot,
    screen: string,
    promptId: string | undefined,
    capturedAt: number,
  ): string {
    const when = parts(capturedAt, this.#timeZone);
    const cursor = (snapshot.cursor ?? {}) as { x?: unknown; y?: unknown };
    const lines = [
      RULE,
      "SCREEN CAPTURE",
      RULE,
      `Timestamp: ${when.year}-${pad(when.month as number)}-${pad(when.day as number)} ${pad(when.hour as number)}:${pad(when.minute as number)}:${pad(when.second as number)}`,
      // Always present: a screen with no hash was refused before this.
      `Hash: ${snapshot.screen_hash}`,
      // A partial cursor is still worth recording; the missing half reads as
      // the origin rather than losing the half that was known.
      `Cursor: (${cursor.x ?? 0}, ${cursor.y ?? 0})`,
      // An 80x25 ANSI terminal is what a reader would assume anyway.
      `Size: ${snapshot.cols ?? 80}x${snapshot.rows ?? 25}`,
      `Terminal: ${snapshot.term ?? "ANSI"}`,
    ];

    if (promptId !== undefined && promptId !== "") {
      lines.push(`Prompt ID: ${promptId}`);
    }
    if (Object.hasOwn(snapshot, "prompt_detected")) {
      const detected = (snapshot.prompt_detected ?? {}) as Record<string, unknown>;
      lines.push(`Input Type: ${detected.input_type ?? "unknown"}`);
      lines.push(`Idle: ${pyBool(detected.is_idle)}`);
    }
    if (snapshot.cursor_at_end !== undefined && snapshot.cursor_at_end !== null) {
      // False is as worth recording as true — it is why a prompt was not
      // matched, where an absent line would read as "not known".
      lines.push(`Cursor at End: ${pyBool(snapshot.cursor_at_end)}`);
    }
    if (snapshot.time_since_last_change !== undefined && snapshot.time_since_last_change !== null) {
      lines.push(`Time Since Last Change: ${(snapshot.time_since_last_change as number).toFixed(2)}s`);
    }

    // The screen as the caller's own check read it: one with no content was
    // refused before this, so there is nothing to fall back to.
    lines.push(RULE, "", screen);
    return lines.join("\n");
  }

  /** Forget what has been written, so it can be written again. */
  clearSavedHashes(): void {
    this.#savedHashes.clear();
  }

  /** How many distinct screens have been kept. */
  savedCount(): number {
    return this.#savedHashes.size;
  }
}
