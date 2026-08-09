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

/** Absolute path to the repository's cross-language `conformance/` directory. */
export const CONFORMANCE_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "conformance");

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

/**
 * Read a shared cross-language corpus from `conformance/`, by relative path.
 *
 * Like {@link loadSpec}, these files live outside this package and are the
 * contract every port replays; unlike `spec/`, they are generated fuzz
 * corpora rather than hand-written contracts. The path is resolved from this
 * module's own URL, so the loader works regardless of the process's working
 * directory.
 */
export function loadConformance<T>(relativePath: string): T {
  return JSON.parse(readFileSync(join(CONFORMANCE_DIR, relativePath), "utf-8")) as T;
}

/**
 * Narrow a corpus lookup or index access, failing by name if it is absent.
 *
 * Tests reach into golden corpora with `find()` and `[0]`, both of which are
 * typed as possibly-undefined. Writing `record?.field` past that point looks
 * defensive but is not: the optional chain short-circuits to `undefined` and
 * the very next member access or index throws a bare TypeError — biome's
 * `noUnsafeOptionalChaining` flags exactly this. Non-null assertion (`!`)
 * silences the lint while keeping the useless error.
 *
 * These lookups genuinely must succeed: a miss means the corpus was
 * regenerated with an entry renamed or removed, and the useful failure names
 * what went missing rather than reporting a property read on undefined.
 */
export function must<T>(value: T | null | undefined, what: string): T {
  if (value === null || value === undefined) {
    throw new Error(`expected ${what} in the golden corpus, found none`);
  }
  return value;
}
