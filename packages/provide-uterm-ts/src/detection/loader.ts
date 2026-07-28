//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Loading a rule set from wherever it lives.
 *
 * Port of the Python module `provide.uterm.detection.loader`.
 */

import { readFileSync } from "node:fs";
import { parseRuleSet, type RuleSet, RuleValidationError } from "./rules.ts";

/** Options for {@link loadRuleSet}. */
export interface LoadRuleSetOptions {
  /** Read `source` as a path rather than as JSON text. */
  fromFile?: boolean;
}

/** Whether this is already a rule set rather than something to read. */
function isRuleSet(source: RuleSet | string): source is RuleSet {
  return typeof source !== "string";
}

/**
 * Read a rule set from JSON text, a file, or one already in hand.
 *
 * A missing file is named in the message, because an operator with several
 * rules files needs to know which one. Anything that fails to parse or fails
 * validation is reported the same way — the distinction between "not JSON"
 * and "not a rule set" is not one the caller can act on differently.
 *
 * @throws {RuleValidationError} On a missing file, unparseable text, or text
 *   that parses but is not a rule set.
 */
export function loadRuleSet(source: RuleSet | string, options: LoadRuleSetOptions = {}): RuleSet {
  if (isRuleSet(source)) {
    return source;
  }
  if (options.fromFile === true) {
    let text: string;
    try {
      text = readFileSync(source, "utf-8");
    } catch (error) {
      throw new RuleValidationError(`Rules file not found: ${source} (${(error as Error).message})`);
    }
    return loadRuleSet(text);
  }
  let data: unknown;
  try {
    data = JSON.parse(source);
  } catch (error) {
    // Named as a JSON fault rather than folded in with a validation one: text
    // that is not JSON and text that is valid JSON but not a rule set are
    // different mistakes, and an operator fixes them differently.
    throw new RuleValidationError(`Failed to parse rules: not JSON: ${(error as Error).message}`);
  }
  try {
    return parseRuleSet(data);
  } catch (error) {
    throw new RuleValidationError(`Failed to parse rules: ${(error as Error).message}`);
  }
}
