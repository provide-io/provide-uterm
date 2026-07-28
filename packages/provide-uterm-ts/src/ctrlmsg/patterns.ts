//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Registry for `link_patterns` control-channel frames.
 *
 * {@link LinkPattern} is an immutable value object describing one
 * server-driven clickable text decoration. {@link LinkPatternRegistry} tracks
 * the active set for a single owner (session, worker, and so on); callers
 * create one registry per owner and there is no shared global state.
 *
 * Port of the Python module `provide.uterm.control_channel_patterns`.
 */

/** The four things a click can do. */
export type LinkPatternAction = "cmd" | "url" | "key" | "focus";

/** Valid actions, for validation and for the error message. */
const VALID_ACTIONS: readonly LinkPatternAction[] = ["cmd", "focus", "key", "url"];

/** Construction fields for {@link LinkPattern}. */
export interface LinkPatternInit {
  /** JavaScript regex source string. */
  pattern: string;
  /** What happens when the user clicks. */
  action: LinkPatternAction;
  /**
   * Stable identifier used by {@link LinkPatternRegistry.unregister}. When two
   * patterns share an id the later `register` replaces the earlier one.
   */
  id?: string;
  /**
   * Regex flags forwarded to `new RegExp(pattern, flags)`. Defaults to `"g"`;
   * the client ensures `"g"` regardless.
   */
  flags?: string;
  /** Which capture group is the clickable span; 0 is the whole match. */
  group?: number;
  /** Click payload template, with `$1`, `$2`, … substituted from captures. */
  payload?: string;
  /** Hover tooltip template, with the same substitution. */
  hover?: string;
  /**
   * CSS class applied to highlighted ranges. Serialised as `"class"`, which
   * the Python side spells `class_` because `class` is reserved there.
   */
  className?: string;
}

/** An immutable descriptor for one server-driven clickable text pattern. */
export class LinkPattern {
  readonly pattern: string;
  readonly action: LinkPatternAction;
  readonly id: string | undefined;
  readonly flags: string;
  readonly group: number;
  readonly payload: string;
  readonly hover: string;
  readonly className: string;

  constructor(init: LinkPatternInit) {
    if (!VALID_ACTIONS.includes(init.action)) {
      throw new Error(`invalid action ${JSON.stringify(init.action)}; must be one of ${JSON.stringify(VALID_ACTIONS)}`);
    }
    this.pattern = init.pattern;
    this.action = init.action;
    this.id = init.id;
    this.flags = init.flags ?? "g";
    this.group = init.group ?? 0;
    this.payload = init.payload ?? "";
    this.hover = init.hover ?? "";
    this.className = init.className ?? "";
  }

  /**
   * Serialise to the wire-format entry the browser expects.
   *
   * Only non-default and non-empty optional fields are included, to keep
   * frames compact.
   */
  toFrameEntry(): Record<string, unknown> {
    const entry: Record<string, unknown> = { pattern: this.pattern, action: this.action };
    if (this.id !== undefined) {
      entry.id = this.id;
    }
    if (this.flags !== "g") {
      entry.flags = this.flags;
    }
    if (this.group !== 0) {
      entry.group = this.group;
    }
    if (this.payload !== "") {
      entry.payload = this.payload;
    }
    if (this.hover !== "") {
      entry.hover = this.hover;
    }
    if (this.className !== "") {
      entry.class = this.className;
    }
    return entry;
  }
}

/**
 * Active pattern set for one owner.
 *
 * Patterns are stored in insertion order. Registering a pattern whose id is
 * already present replaces the earlier one in its existing slot, so order
 * stays predictable for callers that register once and later refresh the same
 * id. Patterns without an id are appended and cannot be removed individually;
 * use {@link clear} to reset the whole set.
 *
 * A `Map` keyed by id or by a monotonic counter reproduces the reference's
 * `dict` semantics exactly: `Map` preserves insertion order for every key
 * type, where a plain object would reorder the integer-like counter keys
 * ahead of the string ids.
 */
export class LinkPatternRegistry {
  readonly #patterns = new Map<string | number, LinkPattern>();
  #counter = 0;

  /** Add `pattern` to the active set. */
  register(pattern: LinkPattern): void {
    if (pattern.id !== undefined) {
      this.#patterns.set(pattern.id, pattern);
      return;
    }
    this.#patterns.set(this.#counter, pattern);
    this.#counter += 1;
  }

  /**
   * Remove the pattern registered under `id`.
   *
   * @returns `true` when a pattern was found and removed.
   */
  unregister(id: string): boolean {
    return this.#patterns.delete(id);
  }

  /** Remove all patterns and reset the id-less counter. */
  clear(): void {
    this.#patterns.clear();
    this.#counter = 0;
  }

  /** All active patterns, in insertion order. */
  getAll(): LinkPattern[] {
    return [...this.#patterns.values()];
  }

  /**
   * The ready-to-send payload for `encodeControlFrame`.
   *
   * Non-destructive: the registry is unchanged.
   */
  syncPayload(): Record<string, unknown> {
    return {
      type: "link_patterns",
      patterns: this.getAll().map((pattern) => pattern.toFrameEntry()),
    };
  }
}
