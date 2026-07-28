//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Loading the server's TOML configuration.
 *
 * Port of the Python module `provide.uterm.server.config` — what happens
 * between a file on disk and a document ready for validation: parsing it,
 * merging it over the defaults, refusing a section that is not a table, and
 * resolving a recording directory written relative to the file.
 *
 * The schema itself is not here. This is the structural pass that runs before
 * it, and it exists because the errors a schema produces for a malformed
 * document name fields nobody wrote.
 */

import { readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { parse as parseToml } from "smol-toml";

/** A configuration that cannot be loaded. Stands in for the reference's `ValueError`. */
export class ConfigLoadError extends Error {}

/**
 * The sections a document must write as tables.
 *
 * TOML lets a key hold a string where a table was meant, and the schema's
 * complaint about that is phrased in terms of the fields inside — naming the
 * section instead says which line to look at.
 */
export const TABLE_SECTIONS: ReadonlySet<string> = new Set([
  "server",
  "auth",
  "ui",
  "recording",
  "profiles",
  "security",
  "tunnel",
  "webhooks",
  "pam",
  "control_plane",
]);

/**
 * Whether a value is a table, as TOML and the merge both mean it.
 *
 * A date is not one. TOML has a datetime type and a parser hands it back as a
 * native date object, which in a language whose dates *are* objects would
 * otherwise read as a table and be merged field by field.
 */
function isTable(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) && !(value instanceof Date);
}

/**
 * The reference's name for a value's type.
 *
 * The message is read against a TOML file, so it names the types that file
 * could hold rather than the ones this engine has. A whole-valued float is
 * indistinguishable from an integer here, which is the same limit the
 * `pycompat` number handling already records — a document writing `1.0` is
 * reported as an `int`.
 */
function pythonTypeName(value: unknown): string {
  if (value === null || value === undefined) {
    return "NoneType";
  }
  if (Array.isArray(value)) {
    return "list";
  }
  // The reference distinguishes a datetime from a date and a time; this
  // engine has one type for all three, so the name is the general one.
  if (value instanceof Date) {
    return "datetime";
  }
  switch (typeof value) {
    case "string":
      return "str";
    case "boolean":
      return "bool";
    case "number":
      return Number.isInteger(value) ? "int" : "float";
    default:
      return "dict";
  }
}

/**
 * Merge one document over another.
 *
 * Deep, but only where both sides are tables: a partial `[auth]` section has
 * to leave the rest of the defaults standing, while a list replaces outright
 * because half of one list and half of another is not a configuration anybody
 * wrote.
 *
 * Neither argument is touched. The defaults are shared, and a merge that
 * mutated them would leak one load's configuration into the next.
 */
export function deepMerge(
  base: Readonly<Record<string, unknown>>,
  override: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(override)) {
    const existing = merged[key];
    merged[key] = isTable(value) && isTable(existing) ? deepMerge(existing, value) : value;
  }
  return merged;
}

/**
 * The structural pass: check the sections and filter the session list.
 *
 * Runs before any schema, and is the whole reason a malformed document
 * produces a message an operator can act on.
 *
 * @throws {ConfigLoadError} When a section that must be a table is not one.
 */
export function normalizeDocument(document: Readonly<Record<string, unknown>>): Record<string, unknown> {
  const normalized: Record<string, unknown> = { ...document };
  for (const section of TABLE_SECTIONS) {
    if (section in normalized && !isTable(normalized[section])) {
      throw new ConfigLoadError(`[${section}] must be a table (got ${pythonTypeName(normalized[section])})`);
    }
  }
  const sessions = normalized.sessions;
  if (Array.isArray(sessions)) {
    // Dropped rather than refused: the one place the reference is lenient,
    // and deliberately so — one bad entry should not stop a server that has
    // other sessions to serve. A `sessions` value that is not a list at all
    // is left for the schema, which is what knows it should have been one.
    normalized.sessions = sessions.filter((entry) => isTable(entry));
  }
  return normalized;
}

/**
 * Parse a TOML document.
 *
 * @throws {ConfigLoadError} When the text is not TOML.
 */
export function parseTomlDocument(text: string): Record<string, unknown> {
  try {
    return parseToml(text) as Record<string, unknown>;
  } catch (error) {
    // Named as a configuration problem: the parser's own message says what is
    // wrong with the bytes but nothing about which file was being read.
    throw new ConfigLoadError(`invalid TOML: ${(error as Error).message}`);
  }
}

/**
 * Read and normalise a config file.
 *
 * A relative recording directory is resolved against the file rather than the
 * working directory: a config is read from wherever it lives and a server is
 * started from wherever the operator happens to be, so resolving against the
 * process would put recordings somewhere neither of them chose.
 *
 * @throws {ConfigLoadError} When the file cannot be read, is not TOML, or
 *   writes a section as something other than a table.
 */
export function loadServerDocument(path: string): Record<string, unknown> {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch (error) {
    throw new ConfigLoadError(`cannot read config ${path}: ${(error as Error).message}`);
  }

  const document = normalizeDocument(parseTomlDocument(text));
  const recording = document.recording;
  // The absolute test is stated rather than relied on: `resolve` already
  // discards everything before an absolute segment, so dropping it would not
  // change an answer — but "a relative directory is resolved against the
  // file" is the rule, and reading it out of `resolve`'s behaviour is not the
  // same as saying it.
  if (isTable(recording) && typeof recording.directory === "string" && !isAbsolute(recording.directory)) {
    document.recording = { ...recording, directory: resolve(dirname(path), recording.directory) };
  }
  return document;
}
