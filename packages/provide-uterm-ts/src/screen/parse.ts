//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Generic screen parsing utilities for BBS terminals.
 *
 * Reusable screen parsing functions shared across different games and BBS
 * systems. Port of the Python module `provide.uterm.screen` and the Go
 * package `screen`.
 */

import { compilePyPattern } from "../pycompat/index.ts";

/** A full ANSI escape sequence: CSI with a final byte, or a two-character form. */
const ANSI_ESCAPE_PATTERN = /\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])/g;

/**
 * Some BBS/telnet servers occasionally leak bare SGR fragments like `1;31m`
 * at line starts without the ESC prefix. These are stripped only when they
 * look isolated, so a fragment inside a word survives.
 */
const BARE_SGR_PATTERN = /(?:(?<=^)|(?<=\n)|(?<=\r)|(?<=\s))(?:\d{1,3}(?:;\d{1,3})*)m(?=\x1b|\s|$)/g;
/** A bare fragment sitting at the start of a line, ahead of real content. */
const BARE_SGR_LINE_PREFIX_PATTERN = /^(?:\d{1,3}(?:;\d{1,3})*)m(?=[A-Z<])/gm;
/** An angle-bracket action tag, bounded so it cannot swallow a screen. */
const ACTION_TAG_PATTERN = /<([^<>\r\n]{1,80})>/g;

/** Default menu-option pattern: `<A> Option`, `[A] Option`, `(A) Option`. */
const DEFAULT_MENU_PATTERN = "[<\\[\\(]([A-Z0-9])[>\\]\\)]\\s+([^<\\[\\(\\n]+?)(?=\\s*[<\\[\\(]|$)";
/** Default numbered-list pattern: `1. Item` or `1) Item`. */
const DEFAULT_NUMBERED_PATTERN = "^\\s*(\\d+)[\\.\\)]\\s+(.+)$";

/**
 * Normalise terminal text for robust prompt and context parsing.
 *
 * Removes ANSI escape and control sequences, removes isolated bare SGR
 * fragments seen in some BBS server output, and normalises line endings.
 */
export function normalizeTerminalText(text: string): string {
  if (text === "") {
    return "";
  }
  let cleaned = text.replaceAll("\r\n", "\n").replaceAll("\r", "\n");
  cleaned = cleaned.replace(ANSI_ESCAPE_PATTERN, "");
  cleaned = cleaned.replace(BARE_SGR_LINE_PREFIX_PATTERN, "");
  return cleaned.replace(BARE_SGR_PATTERN, "");
}

/** Remove ANSI escape codes from text. Alias of {@link normalizeTerminalText}. */
export function stripAnsi(text: string): string {
  return normalizeTerminalText(text);
}

/**
 * Extract angle-bracket action tags such as `<Move>` from a screen snapshot.
 *
 * Tags are trimmed, de-duplicated case-insensitively keeping the first
 * spelling seen, and capped. A cap below one is raised to one, matching the
 * reference's `max(1, int(max_tags))`.
 */
export function extractActionTags(text: string, maxTags = 8): string[] {
  if (text === "") {
    return [];
  }
  const out: string[] = [];
  const seen = new Set<string>();
  const cap = Math.max(1, Math.trunc(maxTags));
  for (const match of text.matchAll(ACTION_TAG_PATTERN)) {
    const tag = (match[1] as string).trim();
    if (tag === "") {
      continue;
    }
    const key = tag.toLowerCase();
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(tag);
    if (out.length >= cap) {
      break;
    }
  }
  return out;
}

/**
 * Clean a screen for display by dropping padding lines.
 *
 * A line is kept when it has content, or when it is not a run of at least
 * eighty spaces — the shape a padded terminal row takes.
 */
export function cleanScreenForDisplay(screen: string, maxLines = 30): string[] {
  const lines: string[] = [];
  for (const line of screen.split("\n")) {
    if (line.trim() !== "" || !line.startsWith(" ".repeat(80))) {
      lines.push(line);
      if (lines.length >= maxLines) {
        break;
      }
    }
  }
  return lines;
}

/**
 * Extract menu options from screen text.
 *
 * Supports the common `<A> Option`, `[A] Option` and `(A) Option` formats. A
 * custom pattern must supply two capture groups: key and description. An
 * invalid pattern yields nothing rather than raising, matching the
 * reference's `except re.error` guard.
 */
export function extractMenuOptions(screen: string, pattern?: string): Array<[string, string]> {
  const options: Array<[string, string]> = [];
  let compiled: RegExp;
  try {
    compiled = compilePyPattern(pattern ?? DEFAULT_MENU_PATTERN);
  } catch {
    return options;
  }
  for (const match of screen.matchAll(compiled)) {
    const description = (match[2] as string).trim();
    if (description !== "") {
      options.push([match[1] as string, description]);
    }
  }
  return options;
}

/**
 * Extract numbered lists from screen text.
 *
 * Supports the common `1. Item` and `1) Item` formats. A custom pattern must
 * supply two capture groups: number and description. Matching is per line,
 * so an anchored pattern behaves the same as in the reference.
 */
export function extractNumberedList(screen: string, pattern?: string): Array<[string, string]> {
  const options: Array<[string, string]> = [];
  let compiled: RegExp;
  try {
    compiled = compilePyPattern(pattern ?? DEFAULT_NUMBERED_PATTERN);
  } catch {
    return options;
  }
  for (const line of screen.split("\n")) {
    const match = compiled.exec(line);
    compiled.lastIndex = 0;
    if (match === null) {
      continue;
    }
    const description = (match[2] as string).trim();
    if (description !== "") {
      options.push([match[1] as string, description]);
    }
  }
  return options;
}

/**
 * Extract key-value pairs from screen text using caller-supplied patterns.
 *
 * Each pattern needs one capture group and is matched case-insensitively. A
 * pattern that fails to compile is skipped rather than raising, so one bad
 * field cannot lose the others.
 */
export function extractKeyValuePairs(screen: string, patterns: Record<string, string>): Record<string, string> {
  const data: Record<string, string> = {};
  for (const [field, pattern] of Object.entries(patterns)) {
    let compiled: RegExp;
    try {
      compiled = compilePyPattern(pattern);
    } catch {
      continue;
    }
    const insensitive = new RegExp(compiled.source, `${compiled.flags.replace("i", "")}i`);
    const match = insensitive.exec(screen);
    if (match !== null) {
      data[field] = match[1] as string;
    }
  }
  return data;
}
