//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The rule schema an operator writes by hand.
 *
 * Port of the Python module `provide.uterm.detection.rules`.
 *
 * This is the only part of detection a human authors, which makes the
 * defaults and the refusals matter more than usual: the defaults are what
 * every rule file already written means, and a rule that is accepted but
 * malformed is one that looks loaded and never fires.
 */

import { pyReEscape } from "../pycompat/index.ts";

/** How an answer is sent. */
export type InputType = "single_key" | "multi_key" | "any_key" | "menu_choice" | "none";

/** How literally a pattern is read. */
export type MatchMode = "regex" | "contains" | "exact";

/** What a prompt is asking for. */
export type PromptKind = "login_name" | "login_pass" | "game_pass" | "pause" | "confirm" | "menu" | "input" | "unknown";

/** What a flow step does. */
export type ActionKind = "send_keys" | "wait" | "noop";

/** A pattern and how to read it. */
export interface RegexRule {
  pattern: string;
  flags: number;
  match_mode: MatchMode;
}

/** Where the cursor has to be. */
export interface ScreenConstraint {
  expect_cursor_at_end: boolean;
  cursor_row_min: number | null;
  cursor_row_max: number | null;
  cursor_col_min: number | null;
  cursor_col_max: number | null;
}

/** One value to pull out of a reply. */
export interface KVExtractRule {
  field: string;
  regex: string;
  type: string;
  flags: number;
  /**
   * The validation an operator wrote.
   *
   * Spelled `validate` in the file and `validate_rule` here, because the
   * reference cannot name its own model field `validate`. Both spellings are
   * load-bearing: a port reading the file under the internal name silently
   * drops every validation an operator wrote, and one emitting the internal
   * name into a detector pattern hands the extractor a key it does not read.
   */
  validate_rule: Record<string, unknown> | null;
  required: boolean;
}

/** A prompt to recognise. */
export interface PromptRule {
  id: string;
  kind: PromptKind;
  input_type: InputType;
  match: RegexRule;
  screen: ScreenConstraint;
  kv_extract: KVExtractRule[];
  notes: string | null;
  negative_match: RegexRule | null;
  default_action: ActionRule | null;
}

/** One choice on a menu. */
export interface MenuOption {
  key: string;
  label: string;
}

/** A menu to recognise. */
export interface MenuRule {
  id: string;
  title_match: RegexRule | null;
  prompt_match: RegexRule;
  options: MenuOption[];
  notes: string | null;
}

/** How long to wait around a step. */
export interface TimingRule {
  min_wait_ms: number;
  max_wait_ms: number;
  retry_ms: number;
  require_stable_screen: boolean;
}

/** A step in a flow. */
export interface ActionRule {
  id: string;
  kind: ActionKind;
  keys: string | null;
  expects_prompt: string | null;
  timing: TimingRule;
  gate_prompts: string[];
  block_if_matches: RegexRule[];
}

/** A named sequence of steps. */
export interface FlowRule {
  id: string;
  description: string;
  steps: ActionRule[];
}

/** Everything an operator wrote for one target. */
export interface RuleSet {
  version: string;
  game: string;
  prompts: PromptRule[];
  menus: MenuRule[];
  flows: FlowRule[];
  metadata: Record<string, unknown>;
}

/** Raised when a rule set is not one. */
export class RuleValidationError extends Error {}

/**
 * `re.MULTILINE | re.IGNORECASE`, the reference's default.
 *
 * The number travels with each extraction rule rather than being applied
 * here — it is for whoever runs the extraction later, which makes it part of
 * the wire format rather than a local detail.
 */
export const DEFAULT_EXTRACT_FLAGS = 10;

/** The closed sets. Anything outside one is a rule that would never fire. */
const INPUT_TYPES: readonly string[] = ["single_key", "multi_key", "any_key", "menu_choice", "none"];
const MATCH_MODES: readonly string[] = ["regex", "contains", "exact"];
const PROMPT_KINDS: readonly string[] = [
  "login_name",
  "login_pass",
  "game_pass",
  "pause",
  "confirm",
  "menu",
  "input",
  "unknown",
];
const ACTION_KINDS: readonly string[] = ["send_keys", "wait", "noop"];

/** Where in the rule set a problem was found. */
type Path = string;

/** Refuse a rule set, saying where and why. */
function refuse(path: Path, detail: string): never {
  throw new RuleValidationError(`${path}: ${detail}`);
}

/** A plain object, as JSON produces. */
function asObject(value: unknown, path: Path): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return refuse(path, "expected an object");
  }
  return value as Record<string, unknown>;
}

/** A required string. */
function requiredString(source: Record<string, unknown>, key: string, path: Path): string {
  const value = source[key];
  if (typeof value !== "string") {
    return refuse(`${path}.${key}`, "expected a string");
  }
  return value;
}

/** An optional string with a fallback. */
function optionalString(source: Record<string, unknown>, key: string, path: Path, fallback: string): string {
  const value = source[key];
  if (value === undefined) {
    return fallback;
  }
  if (typeof value !== "string") {
    return refuse(`${path}.${key}`, "expected a string");
  }
  return value;
}

/** An optional string that may be absent entirely. */
function nullableString(source: Record<string, unknown>, key: string, path: Path): string | null {
  const value = source[key];
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "string") {
    return refuse(`${path}.${key}`, "expected a string");
  }
  return value;
}

/** A member of a closed set. */
function oneOf<T extends string>(
  source: Record<string, unknown>,
  key: string,
  path: Path,
  allowed: readonly string[],
  fallback: T,
): T {
  const value = source[key];
  if (value === undefined) {
    return fallback;
  }
  if (typeof value !== "string" || !allowed.includes(value)) {
    return refuse(`${path}.${key}`, `expected one of ${allowed.join(", ")}`);
  }
  return value as T;
}

/**
 * A number, reading a numeric string as one.
 *
 * The reference's validator runs in its lax mode, where `"5"` is 5. A rules
 * file that quotes its numbers still loads there, so refusing them here would
 * reject files the reference takes.
 */
function coerceNumber(value: unknown, path: Path): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return refuse(path, "expected a number");
}

/** An optional number with a fallback. */
function optionalNumber(source: Record<string, unknown>, key: string, path: Path, fallback: number): number {
  const value = source[key];
  return value === undefined ? fallback : coerceNumber(value, `${path}.${key}`);
}

/** An optional number that may be absent entirely. */
function nullableNumber(source: Record<string, unknown>, key: string, path: Path): number | null {
  const value = source[key];
  return value === undefined || value === null ? null : coerceNumber(value, `${path}.${key}`);
}

/** An optional boolean with a fallback. */
function optionalBoolean(source: Record<string, unknown>, key: string, path: Path, fallback: boolean): boolean {
  const value = source[key];
  if (value === undefined) {
    return fallback;
  }
  if (typeof value !== "boolean") {
    return refuse(`${path}.${key}`, "expected a boolean");
  }
  return value;
}

/** An optional list, read item by item. */
function optionalList<T>(
  source: Record<string, unknown>,
  key: string,
  path: Path,
  read: (item: unknown, itemPath: Path) => T,
): T[] {
  const value = source[key];
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value)) {
    return refuse(`${path}.${key}`, "expected a list");
  }
  return value.map((item, index) => read(item, `${path}.${key}[${index}]`));
}

/** Read a pattern and how to read it. */
function readRegexRule(value: unknown, path: Path): RegexRule {
  const source = asObject(value, path);
  return {
    pattern: requiredString(source, "pattern", path),
    flags: optionalNumber(source, "flags", path, DEFAULT_EXTRACT_FLAGS),
    match_mode: oneOf<MatchMode>(source, "match_mode", path, MATCH_MODES, "regex"),
  };
}

/** Read where the cursor has to be. */
function readScreenConstraint(value: unknown, path: Path): ScreenConstraint {
  const source = asObject(value, path);
  return {
    expect_cursor_at_end: optionalBoolean(source, "expect_cursor_at_end", path, true),
    cursor_row_min: nullableNumber(source, "cursor_row_min", path),
    cursor_row_max: nullableNumber(source, "cursor_row_max", path),
    cursor_col_min: nullableNumber(source, "cursor_col_min", path),
    cursor_col_max: nullableNumber(source, "cursor_col_max", path),
  };
}

/** Read one value to pull out of a reply. */
function readKVExtractRule(value: unknown, path: Path): KVExtractRule {
  const source = asObject(value, path);
  const validation = source.validate;
  return {
    field: requiredString(source, "field", path),
    regex: requiredString(source, "regex", path),
    type: optionalString(source, "type", path, "string"),
    flags: optionalNumber(source, "flags", path, DEFAULT_EXTRACT_FLAGS),
    validate_rule: validation === undefined || validation === null ? null : asObject(validation, `${path}.validate`),
    required: optionalBoolean(source, "required", path, false),
  };
}

/** Read how long to wait around a step. */
function readTimingRule(value: unknown, path: Path): TimingRule {
  const source = asObject(value, path);
  return {
    min_wait_ms: optionalNumber(source, "min_wait_ms", path, 0),
    max_wait_ms: optionalNumber(source, "max_wait_ms", path, 8000),
    retry_ms: optionalNumber(source, "retry_ms", path, 250),
    require_stable_screen: optionalBoolean(source, "require_stable_screen", path, true),
  };
}

/** Read a step in a flow. */
function readActionRule(value: unknown, path: Path): ActionRule {
  const source = asObject(value, path);
  return {
    id: requiredString(source, "id", path),
    kind:
      oneOf<ActionKind>(source, "kind", path, ACTION_KINDS, undefined as never) ?? refuse(`${path}.kind`, "required"),
    keys: nullableString(source, "keys", path),
    expects_prompt: nullableString(source, "expects_prompt", path),
    timing: readTimingRule(source.timing ?? {}, `${path}.timing`),
    gate_prompts: optionalList(source, "gate_prompts", path, (item, itemPath) =>
      typeof item === "string" ? item : refuse(itemPath, "expected a string"),
    ),
    block_if_matches: optionalList(source, "block_if_matches", path, readRegexRule),
  };
}

/** Read a prompt to recognise. */
function readPromptRule(value: unknown, path: Path): PromptRule {
  const source = asObject(value, path);
  // Named explicitly so the message says "match is required" rather than
  // "expected an object", which is what reading an absent one would say on
  // its own. The refusal is the same either way.
  if (source.match === undefined) {
    refuse(`${path}.match`, "required");
  }
  return {
    id: requiredString(source, "id", path),
    kind: oneOf<PromptKind>(source, "kind", path, PROMPT_KINDS, "unknown"),
    input_type: oneOf<InputType>(source, "input_type", path, INPUT_TYPES, "multi_key"),
    match: readRegexRule(source.match, `${path}.match`),
    screen: readScreenConstraint(source.screen ?? {}, `${path}.screen`),
    kv_extract: optionalList(source, "kv_extract", path, readKVExtractRule),
    notes: nullableString(source, "notes", path),
    negative_match:
      source.negative_match === undefined || source.negative_match === null
        ? null
        : readRegexRule(source.negative_match, `${path}.negative_match`),
    default_action:
      source.default_action === undefined || source.default_action === null
        ? null
        : readActionRule(source.default_action, `${path}.default_action`),
  };
}

/** Read a menu to recognise. */
function readMenuRule(value: unknown, path: Path): MenuRule {
  const source = asObject(value, path);
  // As above: a clearer message for a refusal that would happen regardless.
  if (source.prompt_match === undefined) {
    refuse(`${path}.prompt_match`, "required");
  }
  return {
    id: requiredString(source, "id", path),
    title_match:
      source.title_match === undefined || source.title_match === null
        ? null
        : readRegexRule(source.title_match, `${path}.title_match`),
    prompt_match: readRegexRule(source.prompt_match, `${path}.prompt_match`),
    options: optionalList(source, "options", path, (item, itemPath) => {
      const option = asObject(item, itemPath);
      return { key: requiredString(option, "key", itemPath), label: requiredString(option, "label", itemPath) };
    }),
    notes: nullableString(source, "notes", path),
  };
}

/** Read a named sequence of steps. */
function readFlowRule(value: unknown, path: Path): FlowRule {
  const source = asObject(value, path);
  return {
    id: requiredString(source, "id", path),
    description: requiredString(source, "description", path),
    steps: optionalList(source, "steps", path, readActionRule),
  };
}

/**
 * The expression a rule's match mode resolves to.
 *
 * `regex` is the author's own expression; `contains` is escaped, so a bracket
 * is a bracket rather than a character class; `exact` is escaped *and*
 * anchored, so it matches a whole line rather than appearing anywhere in one.
 * Confusing them silently widens or narrows every rule that uses them.
 */
export function regexRuleToRegex(rule: RegexRule): string {
  if (rule.match_mode === "contains") {
    return pyReEscape(rule.pattern);
  }
  if (rule.match_mode === "exact") {
    return `^${pyReEscape(rule.pattern)}$`;
  }
  return rule.pattern;
}

/**
 * Read and check a rule set.
 *
 * Everything left out is filled in, because the defaults are a contract:
 * every rule file already written against the reference means what they say
 * it means. Everything got wrong is refused rather than guessed at — a rule
 * that looked loaded and never fired is the failure an operator cannot see.
 *
 * @throws {RuleValidationError} On anything that is not a rule set.
 */
export function parseRuleSet(payload: unknown, source?: string): RuleSet {
  const where = source === undefined ? "rules" : `Failed to load rules from ${source}`;
  const root = asObject(payload, where);
  return {
    version: optionalString(root, "version", where, "1.0"),
    game: requiredString(root, "game", where),
    prompts: optionalList(root, "prompts", where, readPromptRule),
    menus: optionalList(root, "menus", where, readMenuRule),
    flows: optionalList(root, "flows", where, readFlowRule),
    metadata: root.metadata === undefined ? {} : asObject(root.metadata, `${where}.metadata`),
  };
}

/**
 * Turn a rule set into detector patterns.
 *
 * The screen constraint moves up to the top level, where the detector reads
 * it, and the match mode is resolved here because the detector compiles what
 * it is given. Menus and flows are not folded in: they are recognised
 * separately, and a menu title as a prompt would answer itself.
 */
export function toPromptPatterns(ruleSet: RuleSet): Array<Record<string, unknown>> {
  return ruleSet.prompts.map((prompt) => {
    const pattern: Record<string, unknown> = {
      id: prompt.id,
      regex: regexRuleToRegex(prompt.match),
      input_type: prompt.input_type,
      expect_cursor_at_end: prompt.screen.expect_cursor_at_end,
      // Text rather than nothing: the detector's diagnostics print this, and
      // an absent value would render as the word "null" in an operator's log.
      notes: prompt.notes ?? "",
      // Says a human wrote this, so a later learning pass does not overwrite it.
      auto_detected: false,
    };
    // Added only when written. The detector reads the exclusion key by
    // presence, so an always-present empty one would be an exclusion.
    if (prompt.negative_match !== null) {
      pattern.negative_regex = regexRuleToRegex(prompt.negative_match);
    }
    if (prompt.kv_extract.length > 0) {
      pattern.kv_extract = prompt.kv_extract.map((item) => ({
        field: item.field,
        regex: item.regex,
        type: item.type,
        flags: item.flags,
        validate: item.validate_rule,
        required: item.required,
      }));
    }
    return pattern;
  });
}
