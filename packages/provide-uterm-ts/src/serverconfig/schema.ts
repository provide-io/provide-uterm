//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What each configuration field will accept.
 *
 * Port of the per-field layer of `provide.uterm.server.config_schema`: the
 * types, the closed sets of choices, and the refusal of any name the schema
 * does not define. The cross-field rules that sit on top of it live in
 * `./validators.ts` and are applied here in the reference's order.
 *
 * A configuration file is where a deployment's posture is set, so a field that
 * quietly takes a value it should not have taken has made a decision on the
 * operator's behalf. Every enumeration here names a *less* safe option
 * alongside its default, which is why a value outside the set is refused
 * rather than falling back to one.
 */

import { SECTION_FIELD_SPECS, TOP_LEVEL_FIELD_SPEC } from "./schema-fields.ts";
import {
  cleanPath,
  validateAuditConfig,
  validateAuthConfig,
  validateControlPlaneConfig,
  validateGovernanceConfig,
  validatePamConfig,
  validateRecordingConfig,
} from "./validators.ts";

/** One complaint about one place in a document, in the reference's own terms. */
export interface ConfigError {
  /** The reference's error class, e.g. `literal_error` or `extra_forbidden`. */
  type: string;
  /** Where it is: section, field, and list index where there is one. */
  loc: (string | number)[];
  /** What an operator reads. */
  msg: string;
}

/** A section the schema defines. */
export type SectionName = keyof typeof SECTION_FIELD_SPECS;

/** A field holding one value of a known type. */
type ScalarKind = "str" | "int" | "float" | "bool" | "path" | "dict";

/** A nested section. */
interface ModelSpec {
  kind: "model";
  optional: boolean;
  name: string;
}

/** What one field will accept, expanded from the shorthand in the spec table. */
type FieldSpec =
  | { kind: ScalarKind; optional: boolean }
  | { kind: "literal"; optional: boolean; choices: readonly string[] }
  | { kind: "list"; optional: boolean; item: { kind: ScalarKind; optional: false } | ModelSpec }
  | ModelSpec;

/** Everything except a nested section, which is checked by a different route. */
type ValueSpec = Exclude<FieldSpec, ModelSpec>;

/** Expand one shorthand code. See the spec table for the notation. */
function expandSpec(code: string | readonly string[]): FieldSpec {
  if (Array.isArray(code)) {
    return { kind: "literal", optional: false, choices: code as readonly string[] };
  }
  let text = code as string;
  const optional = text.endsWith("?");
  if (optional) {
    text = text.slice(0, -1);
  }
  if (text.endsWith("[]")) {
    const inner = text.slice(0, -2);
    return {
      kind: "list",
      optional,
      item: inner.startsWith("model:")
        ? { kind: "model", optional: false, name: inner.slice(6) }
        : { kind: inner as ScalarKind, optional: false },
    };
  }
  if (text.startsWith("model:")) {
    return { kind: "model", optional, name: text.slice(6) };
  }
  return { kind: text as ScalarKind, optional };
}

/** The shorthand table, expanded once. */
const SECTIONS: ReadonlyMap<string, ReadonlyMap<string, FieldSpec>> = new Map(
  Object.entries(SECTION_FIELD_SPECS).map(([section, fields]) => [
    section,
    new Map(Object.entries(fields).map(([field, code]) => [field, expandSpec(code)])),
  ]),
);

const TOP_LEVEL: ReadonlyMap<string, FieldSpec> = new Map(
  Object.entries(TOP_LEVEL_FIELD_SPEC).map(([field, code]) => [field, expandSpec(code)]),
);

/** What each refusal says. The reference's wording, which operators read. */
const MESSAGES: Readonly<Record<string, string>> = {
  extra_forbidden: "Extra inputs are not permitted",
  string_type: "Input should be a valid string",
  int_type: "Input should be a valid integer",
  int_parsing: "Input should be a valid integer, unable to parse string as an integer",
  int_from_float: "Input should be a valid integer, got a number with a fractional part",
  float_type: "Input should be a valid number",
  float_parsing: "Input should be a valid number, unable to parse string as a number",
  bool_type: "Input should be a valid boolean",
  bool_parsing: "Input should be a valid boolean, unable to interpret input",
  list_type: "Input should be a valid list",
  dict_type: "Input should be a valid dictionary",
  // The class in this one is CPython's own repr of `pathlib.Path`, so it moves
  // with the interpreter the corpus was recorded under.
  path_type: "Input is not a valid path for <class 'pathlib._local.Path'>",
};

/** The words that read as a boolean, and which one each reads as. */
const TRUE_WORDS = new Set(["1", "on", "t", "true", "y", "yes"]);
const FALSE_WORDS = new Set(["0", "off", "f", "false", "n", "no"]);

/** A coerced value, or the class of refusal. */
type Coerced = { value: unknown } | { error: string };

/** Whether this is a table rather than a list or a null. */
function isTable(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Normalise a filesystem path the way `pathlib.Path` does.
 *
 * Duplicate and trailing separators go, a bare `.` component goes, and `..` is
 * kept — resolving it would need the filesystem and would change what the path
 * means when a symlink is in it.
 */
function normalizePath(value: string): string {
  const absolute = value.startsWith("/");
  const parts = value.split("/").filter((part) => part !== "" && part !== ".");
  const joined = parts.join("/");
  if (absolute) {
    return `/${joined}`;
  }
  return joined === "" ? "." : joined;
}

function coerceBool(value: unknown): Coerced {
  if (typeof value === "boolean") {
    return { value };
  }
  if (typeof value === "number") {
    return value === 0 || value === 1 ? { value: value === 1 } : { error: "bool_parsing" };
  }
  if (typeof value === "string") {
    const word = value.trim().toLowerCase();
    if (TRUE_WORDS.has(word)) {
      return { value: true };
    }
    return FALSE_WORDS.has(word) ? { value: false } : { error: "bool_parsing" };
  }
  return { error: "bool_type" };
}

function coerceInt(value: unknown): Coerced {
  if (typeof value === "boolean") {
    return { value: value ? 1 : 0 };
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? { value } : { error: "int_from_float" };
  }
  if (typeof value === "string") {
    return /^[+-]?\d+$/.test(value.trim()) ? { value: Number(value.trim()) } : { error: "int_parsing" };
  }
  return { error: "int_type" };
}

function coerceFloat(value: unknown): Coerced {
  if (typeof value === "boolean") {
    return { value: value ? 1 : 0 };
  }
  if (typeof value === "number") {
    return { value };
  }
  if (typeof value === "string") {
    const text = value.trim();
    return /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(text) ? { value: Number(text) } : { error: "float_parsing" };
  }
  return { error: "float_type" };
}

/** Coerce one scalar or list against its spec. */
function coerceValue(spec: ValueSpec, value: unknown): Coerced {
  if (spec.optional && value === null) {
    return { value: null };
  }
  switch (spec.kind) {
    case "literal":
      // Matched exactly. A choice accepted in the wrong case would let
      // `security.mode = "STRICT"` mean something the schema never defined.
      // Nothing that is not a string can be in the set, so membership alone
      // settles it.
      return spec.choices.includes(value as string) ? { value } : { error: "literal_error" };
    case "bool":
      return coerceBool(value);
    case "int":
      return coerceInt(value);
    case "float":
      return coerceFloat(value);
    case "str":
      return typeof value === "string" ? { value } : { error: "string_type" };
    case "path":
      return typeof value === "string" ? { value: normalizePath(value) } : { error: "path_type" };
    case "dict":
      return isTable(value) ? { value } : { error: "dict_type" };
    default:
      return Array.isArray(value) ? { value } : { error: "list_type" };
  }
}

/** What a refusal says, given its class and where it happened. */
function messageFor(error: string, spec: ValueSpec): string {
  if (spec.kind === "literal" && error === "literal_error") {
    const choices = spec.choices.map((choice) => `'${choice}'`);
    return `Input should be ${choices.slice(0, -1).join(", ")} or ${choices[choices.length - 1]}`;
  }
  return MESSAGES[error] as string;
}

/** A rule one field has to satisfy once its type is right. */
type FieldRule = (value: unknown) => string | undefined;

/** A rule that rewrites a field rather than refusing it. */
type FieldTransform = (value: unknown) => unknown;

const FIELD_TRANSFORMS: Readonly<Record<string, Record<string, FieldTransform>>> = {
  UiConfig: {
    app_path: (value) => cleanPath(value as string, "/app"),
    assets_path: (value) => cleanPath(value as string, "/_terminal"),
  },
};

/** Bounds a value has to be inside, in the reference's wording. */
function notBelow(value: unknown, floor: number, message: string): string | undefined {
  return (value as number) < floor ? message : undefined;
}

const FIELD_RULES: Readonly<Record<string, Record<string, FieldRule>>> = {
  RecordingConfig: {
    max_bytes: (value) =>
      notBelow(value, 0, `recording.max_bytes must be >= 0 (0 = unlimited), got: ${value as number}`),
    retention_s: (value) =>
      notBelow(value, 0, `recording.retention_s must be >= 0 (0 = keep indefinitely), got: ${value as number}`),
  },
  ControlPlaneConfig: {
    reap_interval_s: (value) =>
      notBelow(value, 1, `control_plane.reap_interval_s must be > 0, got: ${value as number}`),
    reap_retention_s: (value) =>
      notBelow(
        value,
        0,
        `control_plane.reap_retention_s must be >= 0 (0 = reap as soon as past expiry), got: ${value as number}`,
      ),
  },
  TunnelConfig: {
    token_ttl_s: (value) => notBelow(value, 60, `tunnel.token_ttl_s must be >= 60, got: ${value as number}`),
  },
};

/** The top-level scalars' own rules. */
const TOP_LEVEL_RULES: Readonly<Record<string, FieldRule>> = {
  max_workers: (value) => notBelow(value, 1, `max_workers must be >= 1, got: ${value as number}`),
};

/**
 * The rules that read more than one field at a time.
 *
 * Run only once every field has been accepted, as the reference runs them: a
 * combination is checked against values that passed, so a rule can never be
 * handed a value its own field refused.
 */
const SECTION_RULES: Readonly<Record<string, (input: Record<string, unknown>) => void>> = {
  AuthConfig: validateAuthConfig,
  AuditConfig: validateAuditConfig,
  RecordingConfig: validateRecordingConfig,
  ControlPlaneConfig: validateControlPlaneConfig,
  PamConfig: validatePamConfig,
  GovernanceConfig: validateGovernanceConfig,
};

/** The fields of a section, or a complaint that there is no such section. */
function fieldsOf(section: string): ReadonlyMap<string, FieldSpec> {
  const fields = SECTIONS.get(section);
  if (fields === undefined) {
    throw new Error(`no such configuration section: ${section}`);
  }
  return fields;
}

/**
 * Check the fields of one table against a spec.
 *
 * Declared fields first, in the order the model declares them, then the names
 * nobody defined — which is the order the reference reports them in, so two
 * operators with the same mistakes read the same report.
 */
function checkFields(
  specs: ReadonlyMap<string, FieldSpec>,
  input: Record<string, unknown>,
  rules: Record<string, FieldRule>,
  transforms: Record<string, FieldTransform>,
): { errors: ConfigError[]; values: Record<string, unknown> } {
  const errors: ConfigError[] = [];
  const values: Record<string, unknown> = {};
  for (const [field, spec] of specs) {
    if (!(field in input)) {
      continue;
    }
    if (spec.kind === "model") {
      errors.push(...sectionErrors(spec, input[field], [field]));
      continue;
    }
    const coerced = coerceValue(spec, input[field]);
    if ("error" in coerced) {
      errors.push({ type: coerced.error, loc: [field], msg: messageFor(coerced.error, spec) });
      continue;
    }
    if (spec.kind === "list" && coerced.value !== null) {
      const itemSpec = spec.item;
      const items: unknown[] = [];
      for (const [index, item] of (coerced.value as unknown[]).entries()) {
        if (itemSpec.kind === "model") {
          errors.push(...sectionErrors(itemSpec, item, [field, index]));
          continue;
        }
        const element = coerceValue(itemSpec, item);
        if ("error" in element) {
          errors.push({ type: element.error, loc: [field, index], msg: messageFor(element.error, itemSpec) });
        } else {
          items.push(element.value);
        }
      }
      values[field] = items;
      continue;
    }
    const transform = transforms[field];
    const value = transform === undefined ? coerced.value : transform(coerced.value);
    const complaint = rules[field]?.(value);
    if (complaint !== undefined) {
      errors.push({ type: "value_error", loc: [field], msg: `Value error, ${complaint}` });
      continue;
    }
    values[field] = value;
  }
  for (const field of Object.keys(input)) {
    if (!specs.has(field)) {
      errors.push({ type: "extra_forbidden", loc: [field], msg: MESSAGES.extra_forbidden as string });
    }
  }
  return { errors, values };
}

/**
 * Everything wrong with one section, or an empty list.
 *
 * @throws {Error} When there is no such section — a caller's bug, not an
 *   operator's.
 */
export function validateSection(section: string, input: Record<string, unknown>): ConfigError[] {
  const { errors, values } = checkFields(
    fieldsOf(section),
    input,
    FIELD_RULES[section] ?? {},
    FIELD_TRANSFORMS[section] ?? {},
  );
  if (errors.length > 0) {
    return errors;
  }
  try {
    SECTION_RULES[section]?.(values);
  } catch (error) {
    return [{ type: "value_error", loc: [], msg: `Value error, ${(error as Error).message}` }];
  }
  return [];
}

/**
 * One section's values as the reference would hold them.
 *
 * Only the fields the input gave: a caller wanting the rest merges these over
 * {@link SERVER_CONFIG_DEFAULTS}, which is what the loader does.
 *
 * @throws {Error} When there is no such section.
 */
export function coerceSection(section: string, input: Record<string, unknown>): Record<string, unknown> {
  return checkFields(fieldsOf(section), input, FIELD_RULES[section] ?? {}, FIELD_TRANSFORMS[section] ?? {}).values;
}

/**
 * Everything wrong with a whole document, said in terms of where it is.
 *
 * Session definitions are checked as far as their shape and no further; their
 * own validation — the identifier pattern, the connector types, the folding of
 * unknown keys into `connector_config` — is a separate unit.
 */
export function validateServerConfig(document: Record<string, unknown>): ConfigError[] {
  return checkFields(TOP_LEVEL, document, TOP_LEVEL_RULES, {}).errors;
}

/** One nested section, with its complaints placed under the section's name. */
function sectionErrors(spec: ModelSpec, value: unknown, prefix: (string | number)[]): ConfigError[] {
  if (!isTable(value)) {
    return [{ type: "model_type", loc: prefix, msg: `Input should be a valid dictionary or instance of ${spec.name}` }];
  }
  if (spec.name === "SessionDefinition") {
    // Checked as far as its shape and no further: its own rules — the
    // identifier pattern, the connector types, the folding of unknown keys
    // into `connector_config` — are a separate unit.
    return [];
  }
  return validateSection(spec.name, value).map((error) => ({ ...error, loc: [...prefix, ...error.loc] }));
}
