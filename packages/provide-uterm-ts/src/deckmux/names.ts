//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Derived display names and colours.
 *
 * Port of the Python module `provide.uterm.deckmux._names`.
 *
 * A connection's name and colour come from a hash of its id rather than from
 * anywhere they are stored, so two servers — or a server and a reconnecting
 * browser — arrive at the same answer without coordinating. That makes the
 * derivation a wire format: the tables below, their order, and the bits each
 * half reads are all load-bearing.
 */

import { createHash } from "node:crypto";

/** The adjective half of a derived name. */
export const ADJECTIVES: readonly string[] = [
  "red",
  "blue",
  "green",
  "amber",
  "silver",
  "coral",
  "jade",
  "onyx",
  "pearl",
  "ruby",
  "gold",
  "iron",
  "copper",
  "bronze",
  "crystal",
  "storm",
  "frost",
  "ember",
  "dusk",
  "dawn",
  "ash",
  "moss",
  "slate",
  "flint",
  "cedar",
  "birch",
  "maple",
  "sage",
  "thorn",
  "drift",
  "spark",
  "blaze",
];

/** The animal half of a derived name. */
export const ANIMALS: readonly string[] = [
  "fox",
  "hawk",
  "wolf",
  "otter",
  "lynx",
  "crane",
  "bear",
  "deer",
  "eagle",
  "raven",
  "heron",
  "viper",
  "shark",
  "whale",
  "tiger",
  "panther",
  "falcon",
  "condor",
  "bison",
  "moose",
  "cobra",
  "gecko",
  "puma",
  "osprey",
  "badger",
  "ferret",
  "marten",
  "jackal",
  "ibis",
  "newt",
  "pike",
  "wren",
  "tanuki",
];

/** The palette a participant's colour comes from. */
export const COLORS: readonly string[] = [
  "#e74c3c",
  "#3498db",
  "#2ecc71",
  "#9b59b6",
  "#e67e22",
  "#1abc9c",
  "#f39c12",
  "#e91e63",
  "#00bcd4",
  "#8bc34a",
  "#ff5722",
  "#607d8b",
];

/**
 * A connection id's digest, read as a 256-bit integer.
 *
 * The whole digest, not a prefix of it: the adjective comes from the low bits
 * and the animal from bits 8 and up, so a narrower read renames both halves.
 */
export function hashInt(value: string): bigint {
  return BigInt(`0x${createHash("sha256").update(value, "utf8").digest("hex")}`);
}

/** Pick from a table by a hash, as an index rather than a bigint. */
function pick(table: readonly string[], hash: bigint): string {
  return table[Number(hash % BigInt(table.length))] as string;
}

/** Title-case one lowercase table word. */
function titled(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

/** The display name a connection id derives to. */
export function generateName(connectionId: string): string {
  const hash = hashInt(connectionId);
  return `${titled(pick(ADJECTIVES, hash))} ${titled(pick(ANIMALS, hash >> 8n))}`;
}

/**
 * A colour for a connection, avoiding the ones already in use.
 *
 * Walks forward from its natural slot until it finds a free colour, because
 * two participants rendered the same colour cannot be told apart. When every
 * colour is taken it hands back the natural one anyway: a duplicate colour
 * still lets somebody join, where refusing would leave them unrenderable.
 */
export function generateColor(connectionId: string, taken: ReadonlySet<string> = new Set()): string {
  const hash = hashInt(connectionId);
  const start = Number(hash % BigInt(COLORS.length));
  for (let offset = 0; offset < COLORS.length; offset += 1) {
    const color = COLORS[(start + offset) % COLORS.length] as string;
    if (!taken.has(color)) {
      return color;
    }
  }
  return COLORS[start] as string;
}

/**
 * Two characters standing in for a display name.
 *
 * Sliced by character rather than by storage unit — half a surrogate pair is
 * not a character, and would render as a replacement glyph in an avatar.
 */
export function generateInitials(name: string): string {
  // Python's argument-less `str.split()` does two things at once: it breaks on
  // runs of whitespace and it drops the empty pieces. Both are needed, and
  // they overlap — with the filter in place the `+` changes no answer — but
  // each says a separate thing about what a word is.
  const parts = name.split(/\s+/).filter((part) => part !== "");
  if (parts.length >= 2) {
    return `${[...(parts[0] as string)][0]}${[...(parts[1] as string)][0]}`.toUpperCase();
  }
  return [...name].slice(0, 2).join("").toUpperCase();
}
