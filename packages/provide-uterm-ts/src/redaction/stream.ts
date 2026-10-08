//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Rule-driven secret redaction, and the default rules a recording uses.
 *
 * Port of `provide.uterm.server.bridge.hub.redaction.StreamRedactor` and
 * `redaction_defaults.default_rules`. The reference's recording path builds
 * `StreamRedactor(default_rules()).redact` whenever `recording.redact_sensitive`
 * is on — the default — so a port that recorded without this would write to
 * disk the credentials the reference writes as markers.
 *
 * The patterns are the reference's strings verbatim. They are written in the
 * part of `re` syntax ECMAScript shares, including the scoped `(?i:...)`
 * modifier groups the reference uses so that its rules can be joined into one
 * alternation, which Node has accepted since the engine this package requires.
 */

/** One rule: what to look for, and what to put in its place. */
export interface RedactionRule {
  pattern: string;
  /** The reference's model defaults this to `[REDACTED]`. */
  replacement?: string;
}

/** The replacement a rule gets when it names none. */
const DEFAULT_REPLACEMENT = "[REDACTED]";

/** The reference's default recording rules, in its order. */
const DEFAULT_RULES: readonly Required<RedactionRule>[] = [
  // High-confidence, anchored credential formats.
  {
    pattern: String.raw`\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|ASCA)[0-9A-Z]{16}\b`,
    replacement: "[AWS_ACCESS_KEY_REDACTED]",
  },
  {
    pattern: String.raw`(?i:aws[_ -]?secret[_ -]?access[_ -]?key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?)`,
    replacement: "[AWS_SECRET_REDACTED]",
  },
  { pattern: String.raw`\bgh[opusr]_[A-Za-z0-9_]{36,251}\b`, replacement: "[GITHUB_TOKEN_REDACTED]" },
  { pattern: String.raw`\bxox[abeprs]-(?:[0-9]+-){2,}[A-Za-z0-9-]{20,}\b`, replacement: "[SLACK_TOKEN_REDACTED]" },
  { pattern: String.raw`\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`, replacement: "[JWT_REDACTED]" },
  {
    pattern:
      "-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----" +
      String.raw`[\s\S]+?` +
      "-----END (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
    replacement: "[PRIVATE_KEY_REDACTED]",
  },
  // Generic shapes, after the specific ones.
  {
    pattern: String.raw`(?i:\bauthorization\s*:\s*bearer\s+([A-Za-z0-9._\-+/=]+))`,
    replacement: "Authorization: Bearer [REDACTED]",
  },
  {
    pattern: String.raw`(?i:\b(?:password|passwd|pwd)\s*[:=]\s*['\"]?(\S{1,128}?)['\"]?(?=\s|$|,|;|&))`,
    replacement: "[PASSWORD_REDACTED]",
  },
  {
    pattern: String.raw`(?i:\bapi[_-]?key\s*[:=]\s*['\"]?(\S{6,128}?)['\"]?(?=\s|$|,|;|&))`,
    replacement: "[API_KEY_REDACTED]",
  },
  {
    pattern: String.raw`(?i:\btoken\s*[:=]\s*['\"]?(\S{8,256}?)['\"]?(?=\s|$|,|;|&))`,
    replacement: "[TOKEN_REDACTED]",
  },
];

/** The default recording-redaction rules, as a fresh list a caller may extend. */
export function defaultRedactionRules(): RedactionRule[] {
  return DEFAULT_RULES.map((rule) => ({ ...rule }));
}

/** How many capturing groups a pattern has, counted by the engine itself. */
function groupCount(compiled: RegExp): number {
  // An alternation with the empty string always matches, and the match has
  // one slot per group whatever the groups are.
  return (new RegExp(`${compiled.source}|`).exec("") as RegExpExecArray).length - 1;
}

/**
 * Applies a rule set to text in a single pass.
 *
 * The rules are joined into one alternation, each wrapped in its own group, so
 * a span claimed by one rule is not rescanned by the next — the reference's
 * one-pass behaviour, which differs from applying each rule in turn.
 */
export class StreamRedactor {
  readonly #pattern: RegExp | undefined;
  /** The group number each rule's wrapping group has in the joined pattern. */
  readonly #ruleGroups: number[] = [];
  readonly #replacements: string[] = [];

  constructor(rules: readonly RedactionRule[] = []) {
    const sources: string[] = [];
    let group = 1;
    for (const rule of rules) {
      let compiled: RegExp;
      try {
        compiled = new RegExp(rule.pattern);
      } catch {
        // The reference skips a rule `re` will not compile.
        continue;
      }
      sources.push(`(${rule.pattern})`);
      this.#ruleGroups.push(group);
      this.#replacements.push(rule.replacement ?? DEFAULT_REPLACEMENT);
      group += 1 + groupCount(compiled);
    }
    this.#pattern = sources.length === 0 ? undefined : new RegExp(sources.join("|"), "g");
  }

  /**
   * Redact `data`.
   *
   * An arrow property rather than a method because the recorder takes it as a
   * bare function, as the reference passes `redactor.redact`.
   */
  readonly redact = (data: string): string => {
    if (this.#pattern === undefined) {
      return data;
    }
    // Only one alternative can have matched, so exactly one rule's wrapping
    // group is set — the same rule the reference finds from `lastindex`.
    return data.replace(this.#pattern, (...args: unknown[]) => {
      const index = this.#ruleGroups.findIndex((group) => args[group] !== undefined);
      return this.#replacements[index] as string;
    });
  };
}
