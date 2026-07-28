//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Key-value extraction from screen text.
 *
 * Port of the Python module `provide.uterm.detection.extractor`.
 *
 * This turns a screen into the numbers an agent acts on, so every way it can
 * be quietly wrong ends with something acting on a stale or mistyped value.
 * A field that cannot be read is absent rather than zero, and validation is
 * reported alongside the values rather than enforced over them.
 */

import { compilePySearch, pyFloat, pyInt } from "../pycompat/index.ts";

/** One field to pull out of a screen. */
export interface ExtractConfig {
  field?: unknown;
  regex?: unknown;
  type?: unknown;
  validate?: unknown;
  required?: unknown;
  [key: string]: unknown;
}

/** What validation concluded. */
export interface ValidationResult {
  valid: boolean;
  errors: string[];
}

/** Extracted values, with the validation report alongside them. */
export type Extracted = Record<string, unknown> & { _validation?: ValidationResult };

/** The words that mean yes. */
const TRUTHY: ReadonlySet<string> = new Set(["true", "yes", "y", "1", "on"]);

/** The words that mean no. */
const FALSY: ReadonlySet<string> = new Set(["false", "no", "n", "0", "off"]);

/**
 * Convert a matched string to the type a rule asked for.
 *
 * Returns nothing where the reference would raise, because a field that could
 * not be read has to be absent: a zero where the screen said "abc" is worse
 * than nothing, since a caller cannot tell it from a real zero.
 *
 * A type this does not know falls back to text, so a rule author still gets
 * their value.
 */
export function convertType(value: string, targetType: string): unknown {
  const text = value.trim();

  if (targetType === "int") {
    // Separators stripped first: screens write numbers for people to read.
    return pyInt(text.replaceAll(",", ""));
  }
  if (targetType === "float") {
    return pyFloat(text.replaceAll(",", ""));
  }
  if (targetType === "bool") {
    const lowered = text.toLowerCase();
    if (TRUTHY.has(lowered)) {
      return true;
    }
    if (FALSY.has(lowered)) {
      return false;
    }
    // "maybe" is not a decision, and guessing one is how an agent confirms
    // something it was never asked to confirm.
    return undefined;
  }
  return text;
}

/**
 * A field or pattern the config actually supplied.
 *
 * Tested for truthiness rather than for being a string, as the reference
 * does. A field name that is not a string is therefore kept — and skipped by
 * the validation pass, which does check — so such a value reaches a caller
 * unvalidated. Reproduced rather than tightened: a rule set may be relying on
 * the value being present at all.
 */
function supplied(value: unknown): unknown {
  return value === undefined || value === null || value === "" || value === 0 || value === false ? undefined : value;
}

/**
 * Pull one field out of a screen.
 *
 * The **last** match wins. A screen buffer holds scroll history, so the same
 * label appears many times and only the bottom one is current — reading the
 * first would take a credit balance from several screens ago.
 */
function extractSingleField(screen: string, config: ExtractConfig): [PropertyKey, unknown] | undefined {
  const field = supplied(config.field);
  const pattern = supplied(config.regex);
  if (field === undefined || pattern === undefined) {
    return undefined;
  }

  // Case-insensitive and per-line, as the reference compiles them: a screen's
  // case drifts between versions of a program, and a value can be anywhere.
  const compiled = new RegExp(compilePySearch(String(pattern), { ignoreCase: true }).source, "gmi");
  let last: RegExpExecArray | undefined;
  for (const match of screen.matchAll(compiled)) {
    last = match;
  }
  if (last === undefined) {
    return undefined;
  }

  // A capture group is the value when there is one, so a pattern written with
  // parentheses yields the number rather than the label and the number.
  const text = last[1] ?? last[0];
  const converted = convertType(text, typeof config.type === "string" ? config.type : "string");
  return converted === undefined ? undefined : [field as PropertyKey, converted];
}

/** Check one numeric field. */
function validateNumeric(
  field: string,
  value: unknown,
  rules: Record<string, unknown>,
  errors: string[],
  fieldType: string,
): void {
  // A float field accepts a whole number: the reference converted it with
  // `float()`, so 2 is a float there however it prints. Only an int field
  // needs the value to be whole.
  //
  // A boolean passes the int check and fails the float one, because a bool
  // *is* an int in the reference's type system and is not a float. Two
  // configs may name one field, so a value converted as a bool can be
  // validated as a number — refusing it would report an error the reference
  // does not.
  const isNumber = typeof value === "number";
  const acceptable =
    fieldType === "int"
      ? typeof value === "boolean" || (isNumber && Number.isInteger(value))
      : isNumber;
  if (!acceptable) {
    errors.push(`${field}: expected ${fieldType}, got ${describeType(value)}`);
    return;
  }
  const numeric = Number(value);
  if (typeof rules.min === "number" && numeric < rules.min) {
    errors.push(`${field}: value ${numeric} below min ${rules.min}`);
  }
  if (typeof rules.max === "number" && numeric > rules.max) {
    errors.push(`${field}: value ${numeric} exceeds max ${rules.max}`);
  }
}

/** Check one string field. */
function validateString(field: string, value: unknown, rules: Record<string, unknown>, errors: string[]): void {
  if (typeof value !== "string") {
    errors.push(`${field}: expected string, got ${describeType(value)}`);
    return;
  }
  // Anchored at the start and not at the end, because the reference matches
  // rather than fullmatches: "^A" means "begins with A".
  if (typeof rules.pattern === "string" && !new RegExp(`^(?:${compilePySearch(rules.pattern).source})`).test(value)) {
    errors.push(`${field}: value '${value}' does not match pattern ${rules.pattern}`);
  }
  if (Array.isArray(rules.allowed_values) && !rules.allowed_values.includes(value)) {
    errors.push(`${field}: value '${value}' not in allowed values ${pyRepr(rules.allowed_values)}`);
  }
}

/**
 * A list as the reference prints it.
 *
 * The message is what an operator reads, and it is compared against the
 * reference's output — Python renders a list of strings with single quotes
 * and a space after each comma, which JSON does not.
 */
function pyRepr(value: readonly unknown[]): string {
  return `[${value.map((item) => (typeof item === "string" ? `'${item}'` : String(item))).join(", ")}]`;
}

/** The reference's name for a value's type, for an error message. */
function describeType(value: unknown): string {
  if (typeof value === "boolean") {
    return "bool";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? "int" : "float";
  }
  // Only ever a string by here: the conversions produce a string, a number or
  // a boolean, and the other two are named above.
  return "str";
}

/** Check every extracted value against the rules that produced it. */
function validate(extracted: Record<string, unknown>, configs: ExtractConfig[]): ValidationResult {
  const errors: string[] = [];
  for (const config of configs) {
    const field = config.field;
    // A field name that is not a string is skipped here but not during
    // extraction, so such a value reaches a caller unvalidated. Reproduced
    // because a rule set may be relying on the value being present. It
    // happens to reach the same result either way in this language, where an
    // object index is a string regardless — but the reference's dict is not
    // so forgiving, and the skip is what makes the two agree.
    if (typeof field !== "string") {
      continue;
    }
    const value = extracted[field];
    const rules = (config.validate ?? {}) as Record<string, unknown>;
    const fieldType = typeof config.type === "string" ? config.type : "string";

    // Truthiness, as the reference reads it: a rules file writing `1` means
    // required, and treating it as optional would let a missing field pass
    // silently.
    if (config.required && value === undefined) {
      errors.push(`${field}: required but not found`);
      continue;
    }
    if (value === undefined) {
      continue;
    }
    if (fieldType === "int" || fieldType === "float") {
      validateNumeric(field, value, rules, errors, fieldType);
    } else if (fieldType === "string") {
      validateString(field, value, rules, errors);
    }
  }
  // Every problem, not just the first: an operator fixing a rules file wants
  // the whole list rather than one error per run.
  return { valid: errors.length === 0, errors };
}

/**
 * Pull configured values out of a screen.
 *
 * Returns nothing when there was no usable configuration or nothing matched —
 * an empty result and no result are different, and the second tells a caller
 * the screen was not the one they expected.
 *
 * Validation is reported alongside the values rather than enforced over them,
 * so a caller can act on one it knows is out of range, or refuse to. Dropping
 * it would leave them unable to tell "absent" from "implausible".
 */
export function extractKV(
  screen: string,
  config: ExtractConfig | ExtractConfig[] | null | undefined,
  runValidation = true,
): Extracted | undefined {
  if (config === null || config === undefined) {
    return undefined;
  }

  let configs: ExtractConfig[];
  if (Array.isArray(config)) {
    configs = config;
  } else if (typeof config === "object" && Object.hasOwn(config, "field")) {
    configs = [config];
  } else {
    // A lone object with no `field` is not a configuration. The per-field
    // check below would reject it anyway; refusing here says that a bare
    // object was never one, rather than that it produced nothing.
    return undefined;
  }
  if (configs.length === 0) {
    // Likewise: no configurations and no matches both end in nothing, and
    // this says which happened.
    return undefined;
  }

  const extracted: Record<string, unknown> = {};
  for (const entry of configs) {
    const result = extractSingleField(screen, entry);
    if (result !== undefined) {
      extracted[String(result[0])] = result[1];
    }
  }

  if (Object.keys(extracted).length === 0) {
    return undefined;
  }
  if (runValidation) {
    extracted._validation = validate(extracted, configs);
  }
  return extracted;
}
