//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * File I/O helpers for loading BBS screen files and colour palettes, and for
 * opening capture sinks safely.
 *
 * Port of the Python module `provide.uterm.file_io` and the Go package
 * `fileio`.
 */

import {
  chmodSync,
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  writeSync,
} from "node:fs";
import { dirname } from "node:path";
import { DEFAULT_PALETTE } from "../ansi/index.ts";

/** Permission options for the secure-open helpers. */
export interface SecureCreateOptions {
  /** File mode. Defaults to owner read/write only. */
  mode?: number;
  /** Parent directory mode. Defaults to owner-only. */
  dirMode?: number;
}

/**
 * Create `directory` and its parents, then re-apply `mode`.
 *
 * `mkdir`'s mode argument only takes effect when it actually creates the
 * directory, so a pre-existing group-readable recordings directory would keep
 * its bits and leave session filenames enumerable by other local users. The
 * explicit `chmod` afterwards enforces the mode either way.
 */
function ensureOwnerOnlyDir(directory: string, mode: number): void {
  mkdirSync(directory, { mode, recursive: true });
  chmodSync(directory, mode);
}

/**
 * Open `path` for append with owner-only permissions, refusing to follow a
 * symlink.
 *
 * Creating the file through `open` with the mode set avoids the window an
 * `open`-then-`chmod` pair leaves, during which the file is briefly readable
 * by others. `O_NOFOLLOW` means a symlink planted at the target path fails
 * the open rather than redirecting the write, and the regular-file check
 * rejects a FIFO or device planted there instead.
 *
 * @returns An open file descriptor. The caller owns closing it.
 */
export function secureCreate(path: string, options: SecureCreateOptions = {}): number {
  const mode = options.mode ?? 0o600;
  const dirMode = options.dirMode ?? 0o700;
  ensureOwnerOnlyDir(dirname(path), dirMode);
  const fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW, mode);
  try {
    // Defensive, and carried over from the reference. The kernel already
    // rejects a write-open of a directory with EISDIR, and a FIFO blocks
    // rather than returning, so reaching this needs a device node the test
    // suite cannot create unprivileged. It stays as the backstop it is.
    /* v8 ignore next 3 */
    if (!fstatSync(fd).isFile()) {
      throw new Error(`Refusing to open non-regular recording sink: ${path}`);
    }
    // Re-apply the mode: it only takes effect on creation, so a file that
    // already existed keeps whatever bits it had.
    fchmodSync(fd, mode);
  } catch (error) {
    closeSync(fd);
    throw error;
  }
  return fd;
}

/** An append-only text sink. */
export interface AppendHandle {
  /** Append `text` as UTF-8. */
  writeSync(text: string): void;
  /** Close the underlying descriptor. */
  close(): void;
}

/** Open `path` for UTF-8 append using {@link secureCreate}. */
export function secureOpenAppend(path: string, options: SecureCreateOptions = {}): AppendHandle {
  const fd = secureCreate(path, options);
  return {
    writeSync(text: string): void {
      writeSync(fd, Buffer.from(text, "utf-8"));
    },
    close(): void {
      closeSync(fd);
    },
  };
}

/**
 * Load a `.ans` file (BBS ANSI art).
 *
 * Defaults to latin-1, the standard encoding for BBS ANSI art, which maps
 * every byte to a code point so nothing is lost.
 */
export function loadAns(path: string, encoding: BufferEncoding = "latin1"): string {
  return readFileSync(path).toString(encoding);
}

/** Load a plain text file, UTF-8 by default. */
export function loadTxt(path: string, encoding: BufferEncoding = "utf-8"): string {
  return readFileSync(path).toString(encoding);
}

/**
 * Load a JSON 256-colour palette: a list of exactly 16 integers in 0..255.
 *
 * @param path Palette file, or `null` for a copy of the default palette.
 * @throws {Error} If the file is not a list of exactly 16 in-range integers.
 */
export function loadPalette(path: string | null): number[] {
  if (path === null) {
    return [...DEFAULT_PALETTE];
  }
  const data: unknown = JSON.parse(readFileSync(path, "utf-8"));
  if (!Array.isArray(data) || data.length !== 16) {
    throw new Error("palette map must be a JSON list of 16 integers");
  }
  const out: number[] = [];
  for (const value of data) {
    // A boolean is an int in Python, but the reference's isinstance check
    // admits it; refusing it here is the stricter and safer reading, and no
    // real palette carries one.
    if (typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > 255) {
      throw new Error("palette map values must be integers in 0..255");
    }
    out.push(value);
  }
  return out;
}
