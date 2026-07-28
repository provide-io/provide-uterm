//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Pluggable colour-dialect registry.
 *
 * Port of the registry half of `provide.uterm.ansi`. The four built-in
 * dialects are registered at module load, in the same order the reference
 * registers them, because `normalizeColors` runs them in registration order
 * and the order is observable.
 */

import { handleBraceTokens, handleExtendedTokens, handlePipeCodes, handleTildeCodes } from "./dialects.ts";

/** Converts colour tokens of one dialect to ANSI escapes. */
export type ColorDialectHandler = (text: string) => string;

/** Registered dialects, in call order. */
const registry: Array<{ name: string; handler: ColorDialectHandler }> = [];

/**
 * Register a colour token dialect handler.
 *
 * Handlers are called in registration order by {@link normalizeColors}.
 *
 * @throws {Error} If `name` is already registered.
 */
export function registerColorDialect(name: string, handler: ColorDialectHandler): void {
  if (registry.some((entry) => entry.name === name)) {
    throw new Error(`color dialect ${JSON.stringify(name)} is already registered`);
  }
  registry.push({ name, handler });
}

/**
 * Remove a previously registered dialect.
 *
 * @throws {Error} If `name` is not registered.
 */
export function unregisterColorDialect(name: string): void {
  const index = registry.findIndex((entry) => entry.name === name);
  if (index === -1) {
    throw new Error(`color dialect ${JSON.stringify(name)} is not registered`);
  }
  registry.splice(index, 1);
}

/** The names of all registered dialects, in call order. */
export function registeredDialects(): string[] {
  return registry.map((entry) => entry.name);
}

/**
 * Convert all registered BBS colour token formats to standard ANSI escapes.
 *
 * Runs each registered dialect handler in order. The built-in dialects
 * handle `{F###}` / `{B###}` 256-colour tokens, `{P#}` / `{T#}` legacy
 * palette tokens, `~N` tilde codes and `|00`-`|23` pipe codes.
 */
export function normalizeColors(text: string): string {
  let result = text;
  for (const entry of registry) {
    result = entry.handler(result);
  }
  return result;
}

/** Historical alias for {@link normalizeColors}. */
export const previewAnsi = normalizeColors;

registerColorDialect("brace_tokens", handleBraceTokens);
registerColorDialect("extended_tokens", handleExtendedTokens);
registerColorDialect("tilde_codes", handleTildeCodes);
registerColorDialect("pipe_codes", handlePipeCodes);
