//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython-compatible numeric rounding.
 *
 * The reference implementation is Python, so any port that reproduces a
 * quantiser has to reproduce Python's tie-breaking too. `Math.round` rounds
 * halves away from zero; CPython's `round()` rounds halves to even
 * ("banker's rounding") — the same rule Go spells `math.RoundToEven` and C#
 * spells `MidpointRounding.ToEven`.
 */

/**
 * Round `value` to the nearest integer, breaking exact ties towards the even
 * neighbour — the behaviour of CPython's single-argument `round()`.
 *
 * Returns `0` rather than `-0` for values that round to zero, matching
 * CPython's integer result.
 */
export function pyRound(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  let result: number;
  if (diff > 0.5) {
    result = floor + 1;
  } else if (diff < 0.5) {
    result = floor;
  } else {
    result = floor % 2 === 0 ? floor : floor + 1;
  }
  // `Math.floor(-0.4)` is -1 and `-1 + 1` is -0; CPython yields the integer 0.
  return result === 0 ? 0 : result;
}
