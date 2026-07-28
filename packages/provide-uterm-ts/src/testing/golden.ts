//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Loader for the committed differential golden corpora.
 *
 * Each corpus under `testdata/` is produced by a `gen_*_golden.py` script
 * that runs the CPython reference implementation over a deterministic set of
 * inputs. The TypeScript tests replay the same inputs and must match
 * byte-for-byte — the same contract the Go port holds itself to.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Absolute path to the `testdata/` directory of this package. */
export const TESTDATA_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "testdata");

/** Absolute path to the repository's cross-language `spec/` directory. */
export const SPEC_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "spec");

/** Read and parse a JSON golden corpus by file name. */
export function loadGolden<T>(name: string): T {
  return JSON.parse(readFileSync(join(TESTDATA_DIR, name), "utf-8")) as T;
}

/**
 * Read a shared cross-language contract file from `spec/`.
 *
 * These are not this port's own corpora: Go, C# and Python are held to the
 * same file, so a divergence here is a divergence between implementations
 * rather than a stale recording.
 */
export function loadSpec<T>(name: string): T {
  return JSON.parse(readFileSync(join(SPEC_DIR, name), "utf-8")) as T;
}
